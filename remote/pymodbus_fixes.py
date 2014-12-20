
# 
# Cpppo -- Communication Protocol Python Parser and Originator
# 
# Copyright (c) 2013, Hard Consulting Corporation.
# 
# Cpppo is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  See the LICENSE file at the top of the source tree.
# 
# Cpppo is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# 

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

__author__                      = "Perry Kundert"
__email__                       = "perry@hardconsulting.com"
__copyright__                   = "Copyright (c) 2013 Hard Consulting Corporation"
__license__                     = "Dual License: GPLv3 (or later) and Commercial (see LICENSE)"

"""
remote.pymodbus_fixes -- PyModbus has some issues that need fixing
"""
__all__				= ['modbus_server_tcp', 'modbus_server_rtu', 'modbus_rtu_framer_collecting',
                                   'modbus_client_timeout', 'modbus_client_rtu', 'modbus_client_tcp']
import logging
import os
import select
import socket
import sys
import threading
import time
import traceback

import cpppo

# We need to monkeypatch ModbusTcpServer's SocketServer.serve_forever to be
# Python3 socketserver interface-compatible.  When pymodbus is ported to Python3, this
# will not be necessary in the Python3 implementation.
assert sys.version_info.major < 3, "pymodbus is not yet Python3 compatible"
from pymodbus.server.sync import ModbusTcpServer, ModbusSerialServer, ModbusSingleRequestHandler
from SocketServer import _eintr_retry

from pymodbus.transaction import ModbusSocketFramer, ModbusRtuFramer
from pymodbus.constants import Defaults
from pymodbus.client.sync import ModbusTcpClient, ModbusSerialClient
from pymodbus.factory import ClientDecoder
from pymodbus.exceptions import *
from pymodbus.pdu import ExceptionResponse, ModbusResponse
from pymodbus.utilities import checkCRC


class modbus_rtu_framer_collecting( ModbusRtuFramer ):
    """Unfortunately, the standard ModbusSerialServer uses the PySerial Serial.read
    as an equivalent to Socket.recv.  It is not semantically equivalent.  The
    Socket.recv will block and then return all the data available (up to and
    including 1024 bytes) before timeout, which will eventually include a
    complete transaction.  The Serial.read will block 'til its either achieves
    its target number of bytes or times out.

    If ModbusSerialServer instead invoked the recv method with its default
    number of bytes (1, for Serial.read), then this might work; we would
    receive, frame and respond to an incoming request as soon as its last byte
    arrived.  However, ModbusSerialServer calls it with 1024, forcing
    Serial.read to time out -- every request always takes at least
    Defaults.Timeout to arrive (awaiting the next byte after the termination of
    the request, which never arrives)!

    Therefore, we need to patch ModbusSerialServer._build_handler to provide a
    semantically correct recv.  It differs from ModbusSerialClient in that
    receiving nothing is not an error.

    Neither of these are quite right for serial communications, especially
    Modbus/RTU as implemented by ModbusRtuFramer.  Since it discards any
    partially received packet, the recv method must:

    - await the start of a packet
      - in the RTU simulator, with no timeout (or a long timeout)
    - once receiving, continue receiving 'til a full request is read
      - a space of >=1.5 character periods indicates end of the request
      - a worst-case timeout of greater than the maximum request size (eg. double?)

    The standard serial read semantics with a VMIN of 1 (wait 'til at least one
    symbol is received), and a VTIME of 1/baudrate*1.5 would do this perfectly
    -- if VTIME wasn't in units of 1/10th seconds!  This is much too long: at
    115200bps, 1.5 character times (about 10 bits/character) is ~1.5/11520 or
    0.00013 seconds.  The minimum inter-request time is ~3.5/11520 or 0.0003
    seconds!

    So, we must implement such timeouts using select/poll (or, ideally, the
    lower-level RS485-specific IOCTLs, but these are not available except on the
    latest kernels and only in some UART kernel modules.)

    However, the underlying UART receive to select/poll activation may (often
    will) be significantly delayed due to kernel scheduling!  So, not even
    significantly increasing the inter-character timeout to a multiple of the
    inter-message timeout works reliably.  We simply cannot depend on user-level
    timeouts to detect the end of an RS-485 Modbus/RTS frame!

    We might have to detect it statistically.

    After each group of UART input is received immediately when available (with
    *no* inter-character timeout at all), we will attempt to detect a frame.  If
    no frame is available, there are several possibilities:

    1) The frame is incomplete
    2) The frame is corrupt
    3) Some of the leading characters are spurious (noise)

    We can't (of course) know for certain.  However, most often the frame will
    just be incomplete (especially at low baud rates), so we should just wait
    for more characters.  But, we don't want to get locked up on corrupt frames
    or noise, so we don't want to wait forever!  We could use timing (eg. if its
    a long time since last data, throw it out).  But that's hacky, and depends
    on baudrate, which we don't know in the ModbusRtuFramer.

    What is *unlikely* is that there is *another* correct message hidden within
    the valid message.  Just finding a correct CRC for some arbitrary chunk of
    data is P(1/65536).  Finding a full frame with a correct CRC is probably
    pretty unlikely.

    So, keep collecting characters -- never throw them out 'til we find a frame.
    However, each time we get a block of new data, search through it for a valid
    frame.  When one is found, throw out the leading characters (they are noise,
    or an old, corrupted frame), and return the valid frame!


    The fatal issue with statistically trying to frame data, is that there are
    now "forbidden" sequences of data that cannot be used on links of certain
    speeds (eg. where timing will cause the reception of the frame to be broken
    into multiple segments).  This is an impossible situation; the statistical
    approach is a non-starter; we *must* ensure that the complete frame is
    received.  Furthermore, at a higher level (in ModbusTransactionManager's
    .execute), no provision is made for incomplete framing; each frame gets one
    .client._recv, and one .framer.processIncomingPacket.  This would (should?)
    be modified to compute the incoming frame size, and continue reception 'til
    at least the entire frame is received (and validated/rejected) or a timeout
    occurs -- but neither of these details are available to the
    ModbusTransactionManager.


    Therefore, the approach we will take is to ensure that we allow a certain
    amount of time (dependent on the baudrate) for the incoming packet to
    complete.

    """


class modbus_server_tcp( ModbusTcpServer ):
    """Augments the stock pymodbus ModbusTcpServer with the Python3 'socketserver'
    class periodic invocation of the .service_actions() method from within the
    main serve_forever loop.  This allows us to perform periodic service:

        class our_modbus_server( ModbusTcpServerActions ):
            def service_actions( self ):
                logging.info( "Doing something every ~<seconds>" )


        # Start our modbus server, which spawns threads for each new client
        # accepted, and invokes service_actions every ~<seconds> in between.
        modbus = ModbusTcpServerActions()
        modbus.serve_forever( poll_interval=<seconds> )


    The serve_forever implementation comes straight from Python3 socketserver,
    which is basically an enhancement of Python2 SocketServer.

    """
    def serve_forever( self, poll_interval=.5 ):
        self._BaseServer__is_shut_down.clear()
        try:
            while not self._BaseServer__shutdown_request:
                r,w,e 		= _eintr_retry( select.select, [self], [], [], poll_interval )
                if self in r:
                    self._handle_request_noblock()

                self.service_actions()  # <<< Python3 socketserver added this
        finally:
            self._BaseServer__shutdown_request = False
            self._BaseServer__is_shut_down.set()

    def service_actions( self ):
        """Override this to receive service every ~poll_interval s."""
        pass


def modbus_rtu_read( fd, decoder, size=1024, timeout=None ):
    """An fd select.select/os.read loop understands Modbus/RTU protocol, and receives all the data
    available up to the end of the first recognized Modbus/RTU request/reply.  A pymodbus.factory
    ClientDecoder or ServerDecoder must be supplied, in order to recognize the allowed RTU
    requests/replies and compute their sizes.

    """
    incoming			= b''
    begun			= cpppo.timer()
    logging.debug( "Modbus/RTU %s Receive begins  in %7.3f/%7.3fs", decoder.__class__.__name__,
                   cpppo.timer() - begun, timeout if timeout is not None else cpppo.inf )
    complete			= False
    rejected			= 1 # known *not* to be a valid request <function code> ... <crc>
    # Wait up to 'timeout' for an initial request character, then 1/10th second.
    while ( len( incoming ) < size 
            and not complete
            and select.select( [fd], [], [], 1.0/10 if incoming else timeout )[0] ):
        # Not yet satisfied, and frame not yet complete, and still readable; get the next one.  If
        # no input is availabe within 1.5 character times, we should quit; we cannot be anywhere
        # near that precise at the "user" level (probably not even at the device driver level; must
        # be implemented in the RS485-capable UART), so wait a long time (adequate for any UART
        # input available to trickle thru the kernel to the serial input buffer and be reported as
        # ready to receive).  Since we don't know baudrate or kernel loading, we must use an
        # extraordinarily long timeout (1/10th) second.  Unfortunately, this is roughly equivalent
        # to opening the serial port with VMIN=0 and VTIME=1 -- but, astonishingly, there is no way
        # to do that via PySerial!
        c			= os.read( fd, 1 )
        if not c:
            raise SerialException('device reports readiness to read but returned no data (device disconnected or multiple access on port?)')
        incoming	       += c
        logging.debug( "Modbus/RTU %s Receive reading in %7.3f/%7.3fs; %d bytes", decoder.__class__.__name__,
                       cpppo.timer() - begun, timeout if timeout is not None else cpppo.inf,
                       len( incoming ))
        for i in range( rejected, max( rejected, len( incoming ) - 2 )):
            # in a buffer N long, the function code could be anywhere from index 1, to within 3
            # characters from the end: <unit> <func_code> [<data> ...] <crc0> <crc1>.  See if we can
            # reject any more input as definitely *not* a potentially valid request in formation.
            # If its a recognized function code, and we have the complete request data, and its CRC
            # is invalid, reject it and move along.  However, if we cannot yet tell (because we
            # don't yet have a CRC), keep receiving.  For actual ExceptionResponse, we'll just
            # timeout and return what we get (because we can't identify those reliably)
            pdu_class		= decoder.lookupPduClass( ord( incoming[i] ))
            if pdu_class is ExceptionResponse: # Returned for every unrecognized function...
                rejected	= i
                logging.debug( "Modbus/RTU %s Receive rejects in %7.3f/%7.3fs; %d bytes: no frame at offset %d", decoder.__class__.__name__,
                               cpppo.timer() - begun, timeout if timeout is not None else cpppo.inf,
                               len( incoming ), rejected )
                continue
            # Might be a function code!  How big?  Raises Exception if data not yet available.
            try:
                frame_size	= pdu_class.calculateRtuFrameSize( incoming[i-1:] )
                data		= incoming[i-1:i-1+frame_size-2]
                crc		= incoming[i-1+frame_size-2:i-1+frame_size]
                crc_val		= (ord(crc[0]) << 8) + ord(crc[1])
                if checkCRC( data, crc_val ):
                    logging.debug( "Modbus/RTU %s Receive framing in %7.3f/%7.3fs; %d bytes: %s of %d bytes", decoder.__class__.__name__,
                       cpppo.timer() - begun, timeout if timeout is not None else cpppo.inf,
                       len( incoming ), pdu_class.__name__, frame_size )
                    complete	= True
                    break
            except Exception as exc:
                # Not yet possible to tell; keep receiving
                break

    logging.debug( "Modbus/RTU %s Receive success in %7.3f/%7.3fs; %d bytes", decoder.__class__.__name__,
                   cpppo.timer() - begun, timeout if timeout is not None else cpppo.inf,
                   len( incoming ))
    return incoming


class modbus_server_rtu( ModbusSerialServer ):
    def _build_handler( self ):
        request			= self.socket
        request.send		= request.write
        request.recv		= lambda s: modbus_rtu_read(
            fd=self.socket.fd, decoder=self.decoder, timeout=self.socket._timeout )
        handler			= ModbusSingleRequestHandler( request, (self.device, self.device), self )
        return handler


class modbus_client_timeout( object ):
    """Enforces a strict timeout on a complete transaction, including connection and I/O.  The
    beginning of a transaction is indicated by assigning a timeout to the transaction property.  At
    any point, the remaining time available is computed by accessing the transaction property.

    If .timeout is set to True/0, uses Defaults.Timeout around the entire transaction.  If
    transaction is never set or set to None, Defaults.Timeout is always applied to every I/O
    operation, independently (the original behaviour).

    Otherwise, the specified non-zero timeout is applied to the entire transaction.

    If a mutual exclusion lock on a <client> instance is desired (eg. if multiple Threads may be
    attempting to access this client simultaneously, eg. in the case where several independent
    Threads are accessing several slaves via multi-drop serial), it may be obtained using:

        with <client>:
            ...

    Note that such locks will *not* respond to any remaining transaction timeout!

    """
    def __init__( self, *args, **kwargs ):
        super( modbus_client_timeout, self ).__init__( *args, **kwargs )
        self._started	= None
        self._timeout	= None
        self._lock	= threading.Lock()

    @property
    def timeout( self ):
        """Returns the Defaults.Timeout, if no timeout = True|#.# (a hard timeout) has been specified."""
        if self._timeout in (None, True):
            logging.debug( "Transaction timeout default: %.3fs" % ( Defaults.Timeout ))
            return Defaults.Timeout
        now		= cpppo.timer()
        eta		= self._started + self._timeout
        if eta > now:
            logging.debug( "Transaction timeout remaining: %.3fs" % ( eta - now ))
            return eta - now
        logging.debug( "Transaction timeout expired" )
        return 0
    @timeout.setter
    def timeout( self, timeout ):
        """When a self.timeout = True|0|#.# is specified, initiate a hard timeout around the following
        transaction(s).  This means that any connect and/or read/write (_recv) must complete within
        the specified timeout (Defaults.Timeout, if 'True' or 0), starting *now*.  Reset to default
        behaviour with self.timeout = None.

        """
        if timeout is None:
            self._started = None
            self._timeout = None
        else:
            self._started = cpppo.timer()
            self._timeout = ( Defaults.Timeout
                              if ( timeout is True or timeout == 0 )
                              else timeout )

    def __enter__( self ):
        self._lock.acquire( True )
        return self

    def __exit__( self, typ, val, tbk ):
        self._lock.release()
        return False


class modbus_client_tcp( modbus_client_timeout, ModbusTcpClient ):
    """A ModbusTcpClient with transaction timeouts."""
    def connect(self):
        """Duplicate the functionality of connect (handling optional .source_address attribute added
        in pymodbus 1.2.0), but pass the computed remaining timeout.

        """
        if self.socket: return True
        logging.debug( "Connecting to (%s, %s)", getattr( self, 'host', '(serial)' ), self.port )
        begun			= cpppo.timer()
        timeout			= self.timeout # This computes the remaining timeout available
        try:
            self.socket		= socket.create_connection( (self.host, self.port),
                                    timeout=timeout, source_address=getattr( self, 'source_address', None ))
        except socket.error as exc:
            logging.debug('Connection to (%s, %s) failed: %s' % (
                self.host, self.port, exc ))
            self.close()
        finally:
            logging.debug( "Connect completed in %.3fs" % ( cpppo.timer() - begun ))

        return self.socket != None

    def _recv( self, size ):
        """On a receive timeout, closes the socket and raises a ConnectionException.  Otherwise,
        returns the available input."""
        if not self.socket:
            raise ConnectionException( self.__str__() )
        begun			= cpppo.timer()
        timeout			= self.timeout # This computes the remaining timeout available
        logging.debug( "Receive begins  in %7.3f/%7.3fs", cpppo.timer() - begun, timeout )
        r,w,e			= select.select( [self.socket], [], [], timeout )
        if r:
            logging.debug( "Receive reading in %7.3f/%7.3fs", cpppo.timer() - begun, timeout )
            result		= super( modbus_client_timeout, self )._recv( size )
            logging.debug( "Receive success in %7.3f/%7.3fs", cpppo.timer() - begun, timeout )
            return result

        self.close()
        logging.debug( "Receive failure in %7.3f/%7.3fs", cpppo.timer() - begun, timeout )
        raise ConnectionException("Receive from (%s, %s) failed: Timeout" % (
                getattr( self, 'host', '(serial)' ), self.port ))

    def __repr__( self ):
        return "<%s: %s>" % ( self, self.socket.__repr__() if self.socket else "closed" )


class modbus_client_rtu( modbus_client_timeout, ModbusSerialClient ):
    """A ModbusSerialClient wiht timeoust, and semantically correct serial recv, returning up to the
    first detected Modbus/RTU client request or inter-message timeout.

    """
    def __init__( self, method='ascii', framer=None,  **kwargs ):
        '''Initialize a serial client instance.  This is exceedingly gross, but we can't
        easily fix the ModbuSerialClient.__init__ (see BaseModubsClient in
        pymodbus/pymodbus/client/sync.py).  Let it run, then fix the self.framer
        later...  We know that self.transaction is OK, because framer isn't a
        ModbusSocketFramer.

        The methods to connect are::

          - ascii
          - rtu
          - binary

        '''
        # If a 'framer' is supplied, use it (and come up with a self.method name)
        super( modbus_client_rtu, self ).__init__( method=method, **kwargs )

        if framer is not None:
            assert not isinstance( self.framer, ModbusSocketFramer )
            assert not isinstance( framer, ModbusSocketFramer )
            self.method		= framer.__name__
            self.framer		= framer( ClientDecoder() )
            logging.debug( "Fixing ModbusSerialClient framer: %s",  self.method )

    def connect( self ):
        """Reconnect to the serial port, if we've been disconnected (eg. due to poll failure).  Since the
        connect will either immediately succeed or fail, we won't bother implementing a timeout.

        """
        if self.socket: return True
        logging.debug( "Connecting to (%s, %s)", getattr( self, 'host', '(serial)' ), self.port )
        connected		= super( modbus_client_rtu, self ).connect()
        logging.debug( "%r: inter-char timeout: %s", self,
                   self.socket.getInterCharTimeout() if self.socket else None )
        return connected

    def _recv( self, size ):
        """Replicate the approximate semantics of a socket recv; return what's available.  However,
        don't return Nothing (indicating an EOF).  So, wait for up to remaining 'self.timeout'
        for something to show up, but return immediately with whatever is there.

        We'll do it simply -- just read one at a time from the serial port.  We could find out how
        many bytes are available using the TIOCINQ ioctl, but this won't work on non-Posix systems.
        We can't just use the built-in Serial's read method and adjust its own _timeout to reflect
        our own remaining timeout -- we must only block 'til we have at least one character, and
        then continue reading 'til no more input is immediately available; there is no way to invoke
        Serial.read to indicate that.

        """
        if not self.socket:
            raise ConnectionException( self.__str__() )
        begun			= cpppo.timer()
        request			= None
        try:
            request		= modbus_rtu_read( fd=self.socket.fd, decoder=self.framer.decoder,
                                                   size=size, timeout=self.timeout )
        except Exception as exc:
            logging.warning( "Receive Exception %s; %s", exc, traceback.format_exc() )

        if request:
            return request

        # Nothing within timeout; potential client failure, disconnected hardware?  Force a re-open
        self.close()
        logging.debug( "Receive failure in %7.3f/%7.3fs", cpppo.timer() - begun, self.timeout )
        raise ConnectionException("Receive from (%s, %s) failed: Timeout" % (
            getattr( self, 'host', '(serial)' ), self.port ))
