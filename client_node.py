import os, ConfigParser

from twisted.internet import reactor, protocol, stdio, defer
from twisted.protocols import basic
from twisted.internet.protocol import ClientFactory

from common import COMMANDS, display_message, validate_file_md5_hash, get_file_md5_hash, read_bytes_from_file, clean_and_split_input

from dirtools import Dir, DirState
import hashlib
from file_encrypt import encrypt_file, decrypt_file

class CommandLineProtocol(basic.LineReceiver):
	delimiter = '\n'
	
	def __init__(self, server_ip, server_port, files_path):
		self.server_ip = server_ip
		self.server_port = server_port
		self.files_path = files_path
	
	def connectionMade(self):  
		self.factory = FileTransferClientFactory(self.files_path)
		self.connection = reactor.connectTCP(self.server_ip, self.server_port, self.factory)
		self.factory.deferred.addCallback(self._display_response)
		
	def lineReceived(self, line):
		""" If a line is received, call sendCommand(), else prompt user for input. """
		
		if not line:
			self._prompt()
			return
		
		self._sendCommand(line)
		
	def _sendCommand(self, line):
		""" Sends a command to the server. """
		
		data = clean_and_split_input(line) 
		if len(data) == 0 or data == '':
			return 

		command = data[0].lower()
		if not command in COMMANDS:
			self._display_message('Invalid command')
			return
		
		if command == 'list' or command == 'help' or command == 'quit':
			self.connection.transport.write('%s\n' % (command))
		elif command == 'get':
			try:
				filename = data[1]
			except IndexError:
				self._display_message('Missing filename')
				return
			
			self.connection.transport.write('%s %s\n' % (command, filename))
		elif command == 'put':
			try:
				file_path = data[1]
				filename = data[2]
			except IndexError:
				self._display_message('Missing local file path or remote file name')
				return
			
			self.sendFile(file_path, filename)
		
		else:
			self.connection.transport.write('%s %s\n' % (command, data[1]))
		
		self.factory.deferred.addCallback(self._display_response)
			
	def sendFile(self, file_path, filename):
		if not os.path.isfile(file_path):
			self._display_message('This file does not exist')
			return

		file_size = os.path.getsize(file_path) / 1024
	
		print 'Uploading file: %s (%d KB)' % (filename, file_size)
	
		self.connection.transport.write('PUT %s %s\n' % (filename, get_file_md5_hash(file_path)))
		self.setRawMode()
	
		for bytes in read_bytes_from_file(file_path):
			self.connection.transport.write(bytes)
	
		self.connection.transport.write('\r\n')   
	
		# When the transfer is finished, we go back to the line mode 
		self.setLineMode()
	

		
	def _display_response(self, lines = None):
		""" Displays a server response. """
		if lines:
			for line in lines:
				print '%s' % (line)
			
		self._prompt()
		self.factory.deferred = defer.Deferred()
		
	def _prompt(self):
		""" Prompts user for input. """
		self.transport.write('> ')
		
	def _display_message(self, message):
		""" Helper function which prints a message and prompts user for input. """
		
		print message
		self._prompt()	

class FileTransferProtocol(basic.LineReceiver):
	delimiter = '\n'

	def connectionMade(self):
		self.buffer = []
		self.file_handler = None
		self.file_data = ()
		
		print 'Connected to the server'
		
	def connectionLost(self, reason):
		self.file_handler = None
		self.file_data = ()
		
		print 'Connection to the server has been lost'
		reactor.stop()
	
	def lineReceived(self, line):
		if line == 'ENDMSG':
			self.factory.deferred.callback(self.buffer)
			self.buffer = []
		elif line.startswith('HASH'):
			# Received a file name and hash, server is sending us a file
			data = clean_and_split_input(line)

			filename = data[1]
			file_hash = data[2]
			
			self.file_data = (filename, file_hash)
			self.setRawMode()
		else:
			self.buffer.append(line)
		
	def rawDataReceived(self, data):
		filename = self.file_data[0]
		file_path = os.path.join(self.factory.files_path, filename)
		
		print 'Receiving file chunk (%d KB)' % (len(data))
		
		if not self.file_handler:
			self.file_handler = open(file_path, 'wb')
			
		if data.endswith('\r\n'):
			# Last chunk
			data = data[:-2]
			self.file_handler.write(data)
			self.setLineMode()
			
			self.file_handler.close()
			self.file_handler = None
			
			if validate_file_md5_hash(file_path, self.file_data[1]):
				print 'File %s has been successfully transfered and saved' % (filename)
			else:
				os.unlink(file_path)
				print 'File %s has been successfully transfered, but deleted due to invalid MD5 hash' % (filename)
		else:
			self.file_handler.write(data)

class FileTransferClientFactory(protocol.ClientFactory):
	protocol = FileTransferProtocol
	
	def __init__(self, files_path):
		self.files_path = files_path
		self.deferred = defer.Deferred()

def get_dir_changes(directory):
	d = Dir(directory)
	dir_state_new = DirState(d)
	try:
		d2 = Dir('./')
		jsons = d2.files(directory + "*.json")
		jsons.sort(reverse=True)
		dir_state_old = DirState.from_json(jsons[0])
		dir_state_new.to_json()
		return dir_state_new - dir_state_old
	except:
		dir_state_new.to_json()
		return 'new'

def parse_dir_changes(directory, changes, pwd, key):
	if not os.path.exists(directory + '/tmp~'):
		os.makedirs(directory + '/tmp~')
	for file in changes['created'] + changes['updated']:
		if file[-1] == '~':
			continue
		else:
			encrypt_file(key, directory + '/' + file, directory + '/tmp~/' + hashlib.sha256(pwd + file).hexdigest())

def parse_new_dir(directory, pwd, key):
	if not os.path.exists(directory + '/tmp~'):
		os.makedirs(directory + '/tmp~')
	d = Dir(directory)
	for root, dirs, files in d.walk():
		for file in files:
			if file[-1] == '~' or root[-1] == '~':
				continue
			else:
				encrypt_file(key, root + '/' + file, directory + '/tmp~/' + hashlib.sha256(pwd + file).hexdigest())

if __name__ == '__main__':
	config = ConfigParser.ConfigParser()
	config.readfp(open('client.cfg'))
	configport = int(config.get('client', 'port'))
	configpath = config.get('client', 'path')
	configip = config.get('client', 'ip')
	password = config.get('client', 'password')
	enc_key = hashlib.sha256(password).digest()
	
	print 'Client started, incoming files will be saved to %s' % (configpath)
	
	stdio.StandardIO(CommandLineProtocol(configip, configport, configpath))
	reactor.run()
