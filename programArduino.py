import os.path
# Copyright 2012 BrewPi/Elco Jacobs.
# This file is part of BrewPi.

# BrewPi is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# BrewPi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with BrewPi.  If not, see <http://www.gnu.org/licenses/>.

import subprocess as sub
import serial
import time
import simplejson as json
import os
from brewpiVersion import AvrInfo
import expandLogMessage
import settingRestore
from sys import stderr
import BrewPiUtil as util


def printStdErr(string):
	print >> stderr, string + '\n'


def fetchBoardSettings(boardsFile, boardType):
	boardSettings = {}
	for line in boardsFile:
		if line.startswith(boardType):
			setting = line.replace(boardType + '.', '', 1).strip()  # strip board name, period and \n
			[key, sign, val] = setting.rpartition('=')
			boardSettings[key] = val
	return boardSettings


def loadBoardsFile(arduinohome):
	return open(arduinohome + 'hardware/arduino/boards.txt', 'rb').readlines()


def programArduino(config, boardType, hexFile, restoreWhat):
	printStdErr("****    Arduino Program script started    ****")

	arduinohome = config.get('arduinoHome', '/usr/share/arduino/')  # location of Arduino sdk
	avrdudehome = config.get('avrdudeHome', arduinohome + 'hardware/tools/')  # location of avr tools
	avrsizehome = config.get('avrsizeHome', '')  # default to empty string because avrsize is on path
	avrconf = config.get('avrConf', avrdudehome + 'avrdude.conf')  # location of global avr conf

	boardsFile = loadBoardsFile(arduinohome)
	boardSettings = fetchBoardSettings(boardsFile, boardType)
	port = config['port']

	restoreSettings = False
	restoreDevices = False
	if 'settings' in restoreWhat:
		if restoreWhat['settings']:
			restoreSettings = True
	if 'devices' in restoreWhat:
		if restoreWhat['devices']:
			restoreDevices = True
	# Even when restoreSettings and restoreDevices are set to True here,
	# they might be set to false due to version incompatibility later

	printStdErr("Settings will " + ("" if restoreSettings else "not ") + "be restored" +
				(" if possible" if restoreSettings else ""))
	printStdErr("Devices will " + ("" if restoreDevices else "not ") + "be restored" +
				(" if possible" if restoreSettings else ""))

	# open serial port to read old settings and version
	try:
		ser = serial.Serial(port, 57600, timeout=0.2)  # a faster timeout is ok here, makes programming process faster
	except serial.SerialException, e:
		print e
		return 0

	printStdErr("Checking old version before programming.")

	avrVersionOld = None

	retries = 0
	requestVersion = True
	while requestVersion:
		for line in ser.readlines():
			if line[0] == 'N':
				data = line.strip('\n')[2:]
				avrVersionOld = AvrInfo(data)
				printStdErr(( "Found Arduino " + str(avrVersionOld.board) +
							" with a " + str(avrVersionOld.shield) + " shield, " +
							"running BrewPi version " + str(avrVersionOld.version) +
							" build " + str(avrVersionOld.build)))
				requestVersion = False
				break
		else:
			ser.write('n')  # request version info
			time.sleep(1)
			retries += 1
			if retries > 5:
				printStdErr(("Warning: Cannot receive version number from Arduino. " +
							 "Your Arduino is either not programmed yet or running a very old version of BrewPi. "
							 "Arduino will be reset to defaults."))
				break


	oldSettings = {}

	printStdErr("Requesting old settings from Arduino...")
	# request all settings from board before programming
	if avrVersionOld is not None:
		if avrVersionOld.minor > 1:  # older versions did not have a device manager
			ser.write("d{}")  # installed devices
			time.sleep(1)
		ser.write("c")  # control constants
		ser.write("s")  # control settings
		time.sleep(2)


	for line in ser.readlines():
		try:
			if line[0] == 'C':
				oldSettings['controlConstants'] = json.loads(line[2:])
			elif line[0] == 'S':
				oldSettings['controlSettings'] = json.loads(line[2:])
			elif line[0] == 'd':
				oldSettings['installedDevices'] = json.loads(line[2:])

		except json.decoder.JSONDecodeError, e:
			printStdErr("JSON decode error: " + str(e))
			printStdErr("Line received was: " + line)

	ser.close()
	del ser  # Arduino won't reset when serial port is not completely removed
	oldSettingsFileName = 'oldAvrSettings-' + time.strftime("%b-%d-%Y-%H-%M-%S") + '.json'
	printStdErr("Saving old settings to file " + oldSettingsFileName)

	scriptDir = util.scriptPath()  # <-- absolute dir the script is in
	if not os.path.exists(scriptDir + '/settings/avr-backup/'):
		os.makedirs(scriptDir + '/settings/avr-backup/')

	oldSettingsFile = open(scriptDir + '/settings/avr-backup/' + oldSettingsFileName, 'wb')
	oldSettingsFile.write(json.dumps(oldSettings))

	oldSettingsFile.truncate()
	oldSettingsFile.close()

	printStdErr("Loading programming settings from board.txt")

	# parse the Arduino board file to get the right program settings
	for line in boardsFile:
		if line.startswith(boardType):
			# strip board name, period and \n
			setting = line.replace(boardType + '.', '', 1).strip()
			[key, sign, val] = setting.rpartition('=')
			boardSettings[key] = val

	printStdErr("Checking hex file size...")

	# start programming the Arduino
	avrsizeCommand = avrsizehome + 'avr-size ' + hexFile
	printStdErr(avrsizeCommand)
	# check program size against maximum size
	p = sub.Popen(avrsizeCommand, stdout=sub.PIPE, stderr=sub.PIPE, shell=True)
	output, errors = p.communicate()
	if errors != "":
		printStdErr('avr-size error: ' + errors)
		return 0

	programSize = output.split()[7]
	printStdErr(('Progam size: ' + programSize +
		' bytes out of max ' + boardSettings['upload.maximum_size']))

	# Another check just to be sure!
	if int(programSize) > int(boardSettings['upload.maximum_size']):
		printStdErr("ERROR: program size is bigger than maximum size for your Arduino " + boardType)
		return 0

	hexFileDir = os.path.dirname(hexFile)
	hexFileLocal = os.path.basename(hexFile)

	programCommand = (avrdudehome + 'avrdude' +
				' -F ' +
				' -p ' + boardSettings['build.mcu'] +
				' -c ' + boardSettings['upload.protocol'] +
				' -b ' + boardSettings['upload.speed'] +
				' -P ' + port +
				' -U ' + 'flash:w:' + hexFileLocal +
				' -C ' + avrconf)

	printStdErr("Programming Arduino with avrdude: " + programCommand)

	# open and close serial port at 1200 baud. This resets the Arduino Leonardo
	# the Arduino Uno resets every time the serial port is opened automatically
	if boardType == 'leonardo':
		ser = serial.Serial(port, 1200)
		ser.close()
		time.sleep(1)  # give the bootloader time to start up

	p = sub.Popen(programCommand, stdout=sub.PIPE, stderr=sub.PIPE, shell=True, cwd=hexFileDir)
	output, errors = p.communicate()

	# avrdude only uses stderr, append its output to the returnString
	printStdErr("result of invoking avrdude:\n" + errors)

	printStdErr("avrdude done!")

	printStdErr("Giving the Arduino a few seconds to power up...")
	countDown = 6
	while countDown > 0:
		time.sleep(1)
		countDown -= 1
		printStdErr("Back up in " + str(countDown) + "...")

	try:
		ser = serial.Serial(port, 57600, timeout=1)  # timeout=1 is too slow when waiting on temp sensor reads
	except serial.SerialException, e:
		print e
		printStdErr("Error opening serial port after programming: " + str(e))
		return 0

	printStdErr("Now checking which settings and devices can be restored...")


	# read new version
	avrVersionNew = None
	retries = 0
	requestVersion = True
	while requestVersion:
		for line in ser.readlines():
			if line[0] == 'N':
				data = line.strip('\n')[2:]
				avrVersionNew = AvrInfo(data)
				printStdErr(("Checking new version: Found Arduino " + avrVersionNew.board +
								" with a " + str(avrVersionNew.shield) + " shield, " +
								"running BrewPi version " + str(avrVersionNew.version) +
								" build " + str(avrVersionNew.build) + "\n"))
				requestVersion = False
				break

		else:
			ser.write('n')  # request version info
			time.sleep(1)
			retries += 1
			if retries > 10:
				break


	printStdErr("Resetting EEPROM to default settings")
	ser.write('E')
	time.sleep(5)  # resetting EEPROM takes a while, wait 5 seconds
	while 1:  # read all lines on serial interface
		line = ser.readline()
		if line:  # line available?
			if line[0] == 'D':
				# debug message received
				try:
					expandedMessage = expandLogMessage.expandLogMessage(line[2:])
					printStdErr("Arduino debug message: " + expandedMessage)
				except Exception, e:  # catch all exceptions, because out of date file could cause errors
					printStdErr("Error while expanding log message: " + str(e))
					printStdErr("Arduino debug message was: " + line[2:])
		else:
			break

	if avrVersionNew is None:
		printStdErr(("Warning: Cannot receive version number from Arduino after programming. " +
					 "Something must have gone wrong. Porting settings failed.\n"))
		return 0
	if avrVersionOld is None:
		printStdErr("Could not receive version number from old board, " +
		            "No settings are restored.")
		return 0

	settingsRestoreLookupDict = {}
	if avrVersionNew.major == 0 and avrVersionNew.minor == 2:
		if avrVersionOld.major == 0:
			if avrVersionOld.minor == 0:
				printStdErr("Could not receive version number from old board, " +
			                "resetting to defaults without restoring settings.")
				restoreDevices = False
				restoreSettings = False
			if avrVersionOld.major > 0:
				# version 0.1.x, try to restore most of the settings
				settingsRestoreLookupDict = settingRestore.keys_0_1_x_to_0_2_0
				printStdErr("Settings can be partially restored when going from 0.1.x to 0.2.0")
				restoreDevices = False

			if avrVersionOld.minor == 2:
				# restore settings and devices
				settingsRestoreLookupDict = settingRestore.keys_0_2_0_to_0_2_0
				printStdErr("Settings can be fully restored when going from 0.2.0 to 0.2.0")
	else:
		printStdErr("Sorry, settings can only be restored when updating to BrewPi 0.2.0 or higher")

	if restoreSettings:
		restoredSettings = {}
		ccOld = oldSettings['controlConstants']
		csOld = oldSettings['controlSettings']

		ser.write('c')
		ser.write('s')
		time.sleep(2)
		ccNew = {}
		csNew = {}
		while 1:  # read all lines on serial interface
			line = ser.readline()
			if line:  # line available?
				try:
					if line[0] == 'C':
						ccNew = json.loads(line[2:])
					elif line[0] == 'S':
						csNew = json.loads(line[2:])
					elif line[0] == 'D':
						try:  # debug message received
							expandedMessage = expandLogMessage.expandLogMessage(line[2:])
							printStdErr(expandedMessage)
						except Exception, e:  # catch all exceptions, because out of date file could cause errors
							printStdErr("Error while expanding log message: " + str(e))
							printStdErr("Arduino debug message: " + line[2:])
				except json.decoder.JSONDecodeError, e:
						printStdErr("JSON decode error: " + str(e))
						printStdErr("Line received was: " + line)
			else:
				break

		printStdErr("Trying to restore old control constants and settings")
		# find control constants to restore
		for key in ccNew.keys():  # for all new keys
			for alias in settingRestore.getAliases(settingsRestoreLookupDict, key):  # get the valid aliases of old keys
				if alias in ccOld.keys():  # if they are in the old settings
					restoredSettings[key] = ccOld[alias]  # add the old setting to the restoredSettings

		# find control settings to restore
		for key in csNew.keys():  # for all new keys
			for alias in settingRestore.getAliases(settingsRestoreLookupDict, key):  # get the valid aliases of old keys
				if alias in csOld.keys():  # if they are in the old settings
					restoredSettings[key] = csOld[alias]  # add the old setting to the restoredSettings

		printStdErr("Restoring these settings: " + json.dumps(restoredSettings))

		for key in restoredSettings.keys():
			# send one by one or the arduino cannot keep up
			if restoredSettings[key] is not None:
				command = "j{" + str(key) + ":" + str(restoredSettings[key]) + "}\n"
				ser.write(command)
				time.sleep(0.5)
			# read all replies
			while 1:
				line = ser.readline()
				if line:  # line available?
					if line[0] == 'D':
						try:  # debug message received
							expandedMessage = expandLogMessage.expandLogMessage(line[2:])
							printStdErr(expandedMessage)
						except Exception, e:  # catch all exceptions, because out of date file could cause errors
							printStdErr("Error while expanding log message: " + str(e))
							printStdErr("Arduino debug message: " + line[2:])
				else:
					break

		printStdErr("restoring settings done!")
	else:
		printStdErr("No settings to restore!")

	if restoreDevices:
		printStdErr("Now trying to restore previously installed devices: " + str(oldSettings['installedDevices']))
		for device in oldSettings['installedDevices']:
			printStdErr("Restoring device: " + json.dumps(device))
			ser.write("U" + json.dumps(device))

		time.sleep(1)  # give the Arduino time to respond

		# read log messages from arduino
		while 1:  # read all lines on serial interface
			line = ser.readline()
			if line:  # line available?
				if line[0] == 'D':
					try:  # debug message received
						expandedMessage = expandLogMessage.expandLogMessage(line[2:])
						printStdErr(expandedMessage)
					except Exception, e:  # catch all exceptions, because out of date file could cause errors
						printStdErr("Error while expanding log message: " + str(e))
						printStdErr("Arduino debug message: " + line[2:])
				elif line[0] == 'U':
					printStdErr("Arduino reports: device updated to: " + line[2:])
			else:
				break

		printStdErr("Restoring installed devices done!")
	else:
		printStdErr("No devices to restore!")

	printStdErr("****    Program script done!    ****")
	printStdErr("If you started the program script from the web interface, BrewPi will restart automatically")
	ser.close()
	return 1

