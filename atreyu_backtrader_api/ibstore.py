#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
###############################################################################
#
# Copyright (C) 2015-2020 Daniel Rodriguez
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

# import functools
import collections
from copy import copy, deepcopy
from datetime import date, datetime, timedelta
import inspect
import itertools
import random
import threading
import time

from backtrader import TimeFrame, Position
from backtrader.metabase import MetaParams
from backtrader.utils.py3 import bytes, bstr, queue, with_metaclass, long
from backtrader.utils import AutoDict, UTC

import bisect

bytes = bstr  # py2/3 need for ibpy

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum

import logging
logger = logging.getLogger(__name__)

ENABLE_DEBUG=True

def _ts2dt(tstamp=None):
    # Transforms a RTVolume timestamp to a datetime object
    if not tstamp:
        return datetime.utcnow()

    sec, msec = divmod(long(tstamp), 1000)
    usec = msec * 1000
    return datetime.utcfromtimestamp(sec).replace(microsecond=usec)

class ErrorMsg(object):
    def __init__(self, reqId, errorCode, errorString, advancedOrderRejectJson):
        self.vars = vars()
        del self.vars['self']
        self.reqId = reqId
        self.errorCode = errorCode
        self.errorString = errorString
        self.advancedOrderRejectJson = advancedOrderRejectJson

    def __str__(self):
        return f'{self.vars}'

class OpenOrderMsg(object):
    def __init__(self, orderId, contract, order, orderState):
        self.vars = vars()
        del self.vars['self']
        self.orderId = orderId
        self.contract = contract
        self.order = order
        self.orderState = orderState

    def __str__(self):
        return f'{self.vars}'

class OrderStatusMsg(object):
    def __init__(self, orderId , status, filled,
                    remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId,
                    whyHeld, mktCapPrice):
        self.vars = vars()
        self.orderId = orderId
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = avgFillPrice
        self.permId = permId
        self.parentId = parentId
        self.lastFillPrice = lastFillPrice
        self.clientId = clientId
        self.whyHeld = whyHeld
        self.mktCapPrice = mktCapPrice

    def __str__(self):
        return f'{self.vars}'

class RTVolume(object):
    '''Parses a tickString tickType 48 (RTVolume) event from the IB API into its
    constituent fields

    Supports using a "price" to simulate an RTVolume from a tickPrice event
    '''
    _fields = [
        ('price', float),
        ('size', float),
        ('datetime', _ts2dt),
        ('volume', float),
        ('vwap', float),
        ('single', bool)
    ]

    def __init__(self, rtvol='', price=None, tmoffset=None):
        self.vars = vars()
        # Use a provided string or simulate a list of empty tokens
        tokens = iter(rtvol.split(';'))

        # Put the tokens as attributes using the corresponding func
        for name, func in self._fields:
            setattr(self, name, func(next(tokens)) if rtvol else func())

        # If price was provided use it
        if price is not None:
            self.price = price

        if tmoffset is not None:
            self.datetime += tmoffset

    def __str__(self):
        return f'{self.vars}'

class RTPrice(object):
    '''Set price from a tickPrice
    '''
    def __init__(self, price, tmoffset=None):
        self.vars = vars()
        # No size for tickPrice
        self.size = None

        # Set the price
        self.price = price

        # Set price to when we received it
        self.datetime = datetime.now()

        if tmoffset is not None:
            self.datetime += tmoffset

    def __str__(self):
        return f'{self.vars}'

class RTSize(object):
    '''Set size from a tickSize
    '''
    def __init__(self, size, tmoffset=None):
        self.vars = vars()
        # No size for tickPrice
        self.price = None

        # Set the size
        self.size = size

        # Set price to when we received it
        self.datetime = datetime.now()

        if tmoffset is not None:
            self.datetime += tmoffset

    def __str__(self):
        return f'{self.vars}'

class RTBar(object):
    '''Set realtimeBar object
    '''
    def __init__(self, reqId, time, open_, high, low, close, volume, wap, count):
        self.vars = vars()
        self.reqId = reqId
        self.time = time
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.wap = wap
        self.count = count

    def __str__(self):
        return f'{self.vars}'

class HistBar(object):
    '''Set historicalBar object
    '''
    def __init__(self, reqId, bar):
        self.vars = vars()
        self.reqId = reqId
        self.date = bar.date
        self.open = bar.open
        self.high = bar.high
        self.low = bar.low
        self.close = bar.close
        self.volume = bar.volume
        self.wap = bar.wap
        self.count = bar.barCount

    def __str__(self):
        return f'{self.vars}'

class HistTick(object):
    '''Set historicalTick object: 'MIDPOINT', 'BID_ASK', 'TRADES' 
    '''
    def __init__(self, tick, dataType):
        self.vars = vars()
        self.date = datetime.utcfromtimestamp(tick.time)
        self.tickType = tick.tickType if hasattr(tick, 'tickType') else int(0)
        self.dataType = dataType
        if dataType == 'RT_TICK_MIDPOINT':
            self.price = tick.price
        elif dataType == 'RT_TICK_LAST':
            self.price = tick.price
            self.size = float(tick.size)
            self.unreported = tick.tickAttribLast.unreported
            self.pastlimit = tick.tickAttribLast.pastLimit
        elif dataType == 'RT_TICK_BID_ASK':
            self.bidPrice = tick.priceBid
            self.askPrice = tick.priceAsk
            self.bidSize = float(tick.sizeBid)
            self.askSize = float(tick.sizeAsk)
            
        # self.exchange = tick.exchange
        # self.specialconditions = tick.tickAttribLast.specialConditions

    def __str__(self):
        return f'{self.vars}'

class RTTickLast(object):
    '''Set realtimeTick object: 'TRADES' 
    '''
    def __init__(self, tickType, time, price, size, tickAtrribLast, exchange, specialConditions):
        self.vars = vars()
        self.dataType = "RT_TICK_LAST"
        self.datetime = datetime.utcfromtimestamp(time)
        # self.tickType = TickTypeEnum.to_str(tickType)
        self.tickType = tickType
        self.price = price
        self.size = float(size)
        self.pastlimit = tickAtrribLast.pastLimit
        self.unreported = tickAtrribLast.unreported
        # self.exchange = exchange
        # self.specialConditions = specialConditions

    def __str__(self):
        return f'{self.vars}'

class RTTickBidAsk(object):
    '''Set realtimeTick object: 'MIDPOINT', 'BID_ASK', 'TRADES' 
    '''
    def __init__(self, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):
        self.vars = vars()
        self.dataType = "RT_TICK_BID_ASK"
        self.datetime = datetime.utcfromtimestamp(time)
        self.bidPrice = bidPrice
        self.askPrice = askPrice
        self.bidSize = float(bidSize)
        self.askSize = float(askSize)
        self.bidPastLow = tickAttribBidAsk.bidPastLow
        self.askPastHigh = tickAttribBidAsk.askPastHigh

    def __str__(self):
        return f'{self.vars}'

class RTTickMidPoint(object):
    '''Set realtimeTick object: 'MIDPOINT'
    '''
    def __init__(self, time, midPoint):
        self.vars = vars()
        self.dataType = "RT_TICK_MIDPOINT"
        self.datetime = datetime.utcfromtimestamp(time)
        self.midPoint = midPoint

    def __str__(self):
        return f'{self.vars}'

class MetaSingleton(MetaParams):
    '''Metaclass to make a metaclassed class a singleton'''
    def __init__(cls, name, bases, dct):
        super(MetaSingleton, cls).__init__(name, bases, dct)
        cls._singleton = None

    def __call__(cls, *args, **kwargs):
        if cls._singleton is None:
            cls._singleton = (
                super(MetaSingleton, cls).__call__(*args, **kwargs))

        return cls._singleton

def logibmsg(fn):
    def logmsg_decorator(self, *args, **kwargs):
        try:
            if self._debug:
                args_repr = [repr(a) for a in args]
                kwargs_repr = [f"{k}={v!r}" for k, v in kwargs.items()]
                signature = ", ".join(args_repr + kwargs_repr)
                logger.debug(f"Calling {fn.__name__}({signature})")
                print(f"Calling {fn.__name__}({signature})")
            return fn(self, *args, **kwargs)
        except Exception as e:
            logger.exception(f"Exception raised in {fn.__name__}. exception: {str(e)}")
            raise e

    return logmsg_decorator

class IBApi(EWrapper, EClient):
    def __init__(self, cb, _debug):
        EClient.__init__(self, self)
        EWrapper.__init__(self)
        self.cb = cb
        self._debug = _debug

    @logibmsg
    def currentTime(self, time):
        """ Server's current time. This method will receive IB server's system
        time resulting after the invokation of reqCurrentTime. """
        self.cb.currentTime(time)

    @logibmsg
    def updateAccountTime(self, timeStamp):
        logger.debug(f"timeStamp: {timeStamp}")

    @logibmsg
    def nextValidId(self, orderId):
        """ Receives next valid order id."""
        logger.debug(f"nextValidId: {orderId}")
        self.cb.nextValidId(orderId)

    @logibmsg
    def connectAck(self):
        """ callback signifying completion of successful connection """
        self.cb.connectAck()

    @logibmsg
    def connectionClosed(self):
        """This function is called when TWS closes the sockets
        connection with the ActiveX control, or when TWS is shut down."""
        logger.debug(f"connectionClosed")
        self.cb.connectionClosed()

    @logibmsg
    def managedAccounts(self, accountsList):
        """Receives a comma-separated string with the managed account ids."""
        self.cb.managedAccounts(accountsList)

    @logibmsg
    def accountDownloadEnd(self, accountName):
        """This is called after a batch updateAccountValue() and
        updatePortfolio() is sent."""
        self.cb.accountDownloadEnd(accountName)

    @logibmsg
    def updateAccountValue(self, key, val, currency, accountName):
        """ This function is called only when ReqAccountUpdates on
        EEClientSocket object has been called. """
        logger.debug(f"{key}, {val}, {currency}, {accountName}")
        self.cb.updateAccountValue(key, val, currency, accountName)

    @logibmsg
    def updatePortfolio(self, contract, position,
                        marketPrice, marketValue,
                        averageCost, unrealizedPNL,
                        realizedPNL, accountName):
        """This function is called only when reqAccountUpdates on
        EEClientSocket object has been called."""
        self.cb.updatePortfolio(contract, position,
                                    marketPrice, marketValue,
                                    averageCost, unrealizedPNL,
                                    realizedPNL, accountName)

    @logibmsg
    def contractDetails(self, reqId, contractDetails):
        """Receives the full contract's definitions. This method will return all
        contracts matching the requested via EEClientSocket::reqContractDetails.
        For example, one can obtain the whole option chain with it."""
        self.cb.contractDetails(reqId, contractDetails)

    @logibmsg
    def contractDetailsEnd(self, reqId):
        """This function is called once all contract details for a given
        request are received. This helps to define the end of an option
        chain."""
        self.cb.contractDetailsEnd(reqId)

    @logibmsg
    def openOrder(self, orderId, contract, order, orderState):
        """This function is called to feed in open orders.

        orderID: OrderId - The order ID assigned by TWS. Use to cancel or
            update TWS order.
        contract: Contract - The Contract class attributes describe the contract.
        order: Order - The Order class gives the details of the open order.
        orderState: OrderState - The orderState class includes attributes Used
            for both pre and post trade margin and commission data."""
        self.cb.openOrder(OpenOrderMsg(orderId, contract, order, orderState))

    @logibmsg
    def openOrderEnd(self):
        """This is called at the end of a given request for open orders."""
        logger.debug(f"openOrderEnd")
        self.cb.openOrderEnd()

    @logibmsg
    def orderStatus(self, orderId , status, filled,
                    remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId,
                    whyHeld, mktCapPrice):
        """This event is called whenever the status of an order changes. It is
        also fired after reconnecting to TWS if the client has any open orders.

        orderId: OrderId - The order ID that was specified previously in the
            call to placeOrder()
        status:str - The order status. Possible values include:
            PendingSubmit - indicates that you have transmitted the order, but have not  yet received confirmation that it has been accepted by the order destination. NOTE: This order status is not sent by TWS and should be explicitly set by the API developer when an order is submitted.
            PendingCancel - indicates that you have sent a request to cancel the order but have not yet received cancel confirmation from the order destination. At this point, your order is not confirmed canceled. You may still receive an execution while your cancellation request is pending. NOTE: This order status is not sent by TWS and should be explicitly set by the API developer when an order is canceled.
            PreSubmitted - indicates that a simulated order type has been accepted by the IB system and that this order has yet to be elected. The order is held in the IB system until the election criteria are met. At that time the order is transmitted to the order destination as specified.
            Submitted - indicates that your order has been accepted at the order destination and is working.
            Cancelled - indicates that the balance of your order has been confirmed canceled by the IB system. This could occur unexpectedly when IB or the destination has rejected your order.
            Filled - indicates that the order has been completely filled.
            Inactive - indicates that the order has been accepted by the system (simulated orders) or an exchange (native orders) but that currently the order is inactive due to system, exchange or other issues.
        filled:int - Specifies the number of shares that have been executed.
            For more information about partial fills, see Order Status for Partial Fills.
        remaining:int -   Specifies the number of shares still outstanding.
        avgFillPrice:float - The average price of the shares that have been executed. This parameter is valid only if the filled parameter value is greater than zero. Otherwise, the price parameter will be zero.
        permId:int -  The TWS id used to identify orders. Remains the same over TWS sessions.
        parentId:int - The order ID of the parent order, used for bracket and auto trailing stop orders.
        lastFilledPrice:float - The last price of the shares that have been executed. This parameter is valid only if the filled parameter value is greater than zero. Otherwise, the price parameter will be zero.
        clientId:int - The ID of the client (or TWS) that placed the order. Note that TWS orders have a fixed clientId and orderId of 0 that distinguishes them from API orders.
        whyHeld:str - This field is used to identify an order held when TWS is trying to locate shares for a short sell. The value used to indicate this is 'locate'.

        """
        self.cb.orderStatus(OrderStatusMsg(orderId , status, filled,
                                            remaining, avgFillPrice, permId,
                                            parentId, lastFillPrice, clientId,
                                            whyHeld, mktCapPrice))
    
    @logibmsg
    def commissionReport(self, commissionReport):
        """The commissionReport() callback is triggered as follows:
        - immediately after a trade execution
        - by calling reqExecutions()."""
        self.cb.commissionReport(commissionReport)

    @logibmsg
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson = ""):
        self.cb.error(ErrorMsg(reqId, errorCode, errorString, advancedOrderRejectJson))

    @logibmsg
    def position(self, account, contract, pos, avgCost):
        """This event returns real-time positions for all accounts in
        response to the reqPositions() method."""
        self.cb.position(account, contract, pos, avgCost)

    @logibmsg
    def positionEnd(self):
        """This is called once all position data for a given request are
        received and functions as an end marker for the position() data. """
        self.cb.positionEnd()

    @logibmsg
    def tickPrice(self, reqId, tickType, price, attrib):
        """Market data tick price callback. Handles all price related ticks."""
        self.cb.tickPrice(reqId, tickType, price, attrib)

    @logibmsg
    def tickSize(self, reqId, tickType, size):
        """Market data tick size callback. Handles all size-related ticks."""
        self.cb.tickSize(reqId, tickType, size)

    @logibmsg
    def tickGeneric(self, reqId, tickType, value):
        self.cb.tickGeneric(reqId, tickType, value)

    @logibmsg
    def realtimeBar(self, reqId, time, open_, high, low, close, volume, wap, count):
        self.cb.realtimeBar(RTBar(reqId, time, open_, high, low, close, float(volume), wap, count))

    @logibmsg
    def historicalData(self, reqId, bar):
        self.cb.historicalData(HistBar(reqId, bar))

    @logibmsg
    def historicalDataUpdate(self, reqId, bar):
        '''Not implemented'''
        pass

    @logibmsg
    def historicalDataEnd(self, reqId, start, end):
        """ Marks the ending of the historical bars reception. """
        self.cb.historicalDataEnd(reqId, start, end)

    @logibmsg
    def execDetails(self, reqId, contract, execution):
        """This event is fired when the reqExecutions() functions is
        invoked, or when an order is filled.  """
        self.cb.execDetails(reqId, contract, execution)

    @logibmsg
    def execDetailsEnd(self, reqId):
        """This function is called once all executions have been sent to
        a client in response to reqExecutions()."""
        pass

    @logibmsg
    def historicalTicks(self, reqId, ticks, done):
        """For whatToShow=MIDPOINT
        """
        for tick in ticks:
            self.cb.historicalTicks(reqId, HistTick(tick, 'RT_TICK_MIDPOINT'))

    @logibmsg
    def historicalTicksBidAsk(self, reqId, ticks, done):
        """returns historical tick data when whatToShow=BID_ASK"""
        for tick in ticks:
            self.cb.historicalTicks(reqId, HistTick(tick, 'RT_TICK_BID_ASK'))

    @logibmsg
    def historicalTicksLast(self, reqId, ticks, done):
        """returns tick-by-tick data for tickType = "Last" or "AllLast" """
        for tick in ticks:
            self.cb.historicalTicks(reqId, HistTick(tick, 'RT_TICK_LAST'))

    @logibmsg
    def tickByTickAllLast(self, reqId, tickType, time, price, size, tickAtrribLast, exchange, specialConditions):
        """returns tick-by-tick data for tickType = "Last" or "AllLast" """
        self.cb.tickByTickAllLast(reqId, tickType, time, price, size, tickAtrribLast, exchange, specialConditions)

    @logibmsg
    def tickByTickBidAsk(self, reqId, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):
        """returns tick-by-tick data for tickType = "BidAsk" """
        self.cb.tickByTickBidAsk(reqId, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk)

    @logibmsg
    def tickByTickMidPoint(self, reqId, time, midPoint):
        """returns tick-by-tick data for tickType = "MidPoint" """
        self.cb.tickByTickBidAsk(reqId, time, midPoint)

    @logibmsg
    def tickString(self, reqId, tickType, value):
        self.cb.tickString(reqId, tickType, value)


class IBStore(with_metaclass(MetaSingleton, object)):
    '''Singleton class wrapping an ibpy ibConnection instance.

    The parameters can also be specified in the classes which use this store,
    like ``IBData`` and ``IBBroker``

    Params:

      - ``host`` (default:``127.0.0.1``): where IB TWS or IB Gateway are
        actually running. And although this will usually be the localhost, it
        must not be

      - ``port`` (default: ``7496``): port to connect to. The demo system uses
        ``7497``

      - ``clientId`` (default: ``None``): which clientId to use to connect to
        TWS.

        ``None``: generates a random id between 1 and 65535
        An ``integer``: will be passed as the value to use.

      - ``notifyall`` (default: ``False``)

        If ``False`` only ``error`` messages will be sent to the
        ``notify_store`` methods of ``Cerebro`` and ``Strategy``.

        If ``True``, each and every message received from TWS will be notified

      - ``_debug`` (default: ``False``)

        Print all messages received from TWS as info output

      - ``reconnect`` (default: ``3``)

        Number of attempts to try to reconnect after the 1st connection attempt
        fails

        Set it to a ``-1`` value to keep on reconnecting forever

      - ``timeout`` (default: ``3.0``)

        Time in seconds between reconnection attemps

      - ``timeoffset`` (default: ``True``)

        If True, the time obtained from ``reqCurrentTime`` (IB Server time)
        will be used to calculate the offset to localtime and this offset will
        be used for the price notifications (tickPrice events, for example for
        CASH markets) to modify the locally calculated timestamp.

        The time offset will propagate to other parts of the ``backtrader``
        ecosystem like the **resampling** to align resampling timestamps using
        the calculated offset.

      - ``timerefresh`` (default: ``60.0``)

        Time in seconds: how often the time offset has to be refreshed

      - ``indcash`` (default: ``True``)

        Manage IND codes as if they were cash for price retrieval
    '''

    # Set a base for the data requests (historical/realtime) to distinguish the
    # id in the error notifications from orders, where the basis (usually
    # starting at 1) is set by TWS
    REQIDBASE = 0x01000000

    BrokerCls = None  # broker class will autoregister
    DataCls = None  # data class will auto register

    params = (
        ('host', '127.0.0.1'),
        ('port', 7496),
        ('clientId', None),  # None generates a random clientid 1 -> 2^16
        ('broker_host', ''),
        ('broker_request_port', 12345),
        ('broker_subscribe_port', 12345),
        ('broker_user_name', ''),
        ('broker_password', ''),
        ('notifyall', False),
        ('_debug', False),
        ('reconnect', 3),  # -1 forever, 0 No, > 0 number of retries
        ('timeout', 3.0),  # timeout between reconnections
        ('timeoffset', True),  # Use offset to server for timestamps if needed
        ('timerefresh', 60.0),  # How often to refresh the timeoffset
        ('indcash', True),  # Treat IND codes as CASH elements
    )

    @classmethod
    def getdata(cls, *args, **kwargs):
        '''Returns ``DataCls`` with args, kwargs'''
        return cls.DataCls(*args, **kwargs)

    @classmethod
    def getbroker(cls, *args, **kwargs):
        '''Returns broker with *args, **kwargs from registered ``BrokerCls``'''
        return cls.BrokerCls(*args, **kwargs)
    
    def __init__(self):
        super(IBStore, self).__init__()

        self._lock_q = threading.Lock()  # sync access to _tickerId/Queues
        self._lock_accupd = threading.Lock()  # sync account updates
        self._lock_pos = threading.Lock()  # sync account updates
        self._lock_notif = threading.Lock()  # sync access to notif queue
        self._updacclock = threading.Lock()  # sync account updates

        # Account list received
        self._event_managed_accounts = threading.Event()
        self._event_accdownload = threading.Event()

        self.dontreconnect = False  # for non-recoverable connect errors

        self._env = None  # reference to cerebro for general notifications
        self.broker = None  # broker instance
        self.datas = list()  # datas that have registered over start
        self.ccount = 0  # requests to start (from cerebro or datas)

        self._lock_tmoffset = threading.Lock()
        self.tmoffset = timedelta()  # to control time difference with server

        # Structures to hold datas requests
        self.qs = collections.OrderedDict()  # key: tickerId -> queues
        self.ts = collections.OrderedDict()  # key: queue -> tickerId
        self.iscash = dict()  # tickerIds from cash products (for ex: EUR.JPY)

        self.histexreq = dict()  # holds segmented historical requests
        self.histfmt = dict()  # holds datetimeformat for request
        self.histsend = dict()  # holds sessionend (data time) for request
        self.histtz = dict()  # holds sessionend (data time) for request

        self.acc_cash = AutoDict()  # current total cash per account
        self.acc_value = AutoDict()  # current total value per account
        self.acc_upds = AutoDict()  # current account valueinfos per account

        self.port_update = False  # indicate whether to signal to broker

        self.positions = collections.defaultdict(Position)  # actual positions

        self._tickerId = itertools.count(self.REQIDBASE)  # unique tickerIds
        self.orderid = None  # next possible orderid (will be itertools.count)

        self.cdetails = collections.defaultdict(list)  # hold cdetails requests

        self.managed_accounts = list()  # received via managedAccounts

        self.notifs = queue.Queue()  # store notifications for cerebro

        # Use the provided clientId or a random one
        if self.p.clientId is None:
            self.clientId = random.randint(1, pow(2, 16) - 1)
        else:
            self.clientId = self.p.clientId

        self._debug = self.p._debug
        # ibpy connection object
        try:           
            self.conn = IBApi(self, self._debug)
            self.conn.connect(self.p.host, self.p.port, self.clientId)
            self.apiThread = threading.Thread(target=self.conn.run, daemon=True)
            self.apiThread.start()
        except Exception as e:
            print(f"TWS Failed to connect: {e}")

        # This utility key function transforms a barsize into a:
        #   (Timeframe, Compression) tuple which can be sorted
        def keyfn(x):
            n, t = x.split()
            tf, comp = self._sizes[t]
            return (tf, int(n) * comp)

        # This utility key function transforms a duration into a:
        #   (Timeframe, Compression) tuple which can be sorted
        def key2fn(x):
            n, d = x.split()
            tf = self._dur2tf[d]
            return (tf, int(n))

        # Generate a table of reverse durations
        self.revdur = collections.defaultdict(list)
        # The table (dict) is a ONE to MANY relation of
        #   duration -> barsizes
        # Here it is reversed to get a ONE to MANY relation of
        #   barsize -> durations
        for duration, barsizes in self._durations.items():
            for barsize in barsizes:
                self.revdur[keyfn(barsize)].append(duration)

        # Once managed, sort the durations according to real duration and not
        # to the text form using the utility key above
        for barsize in self.revdur:
            self.revdur[barsize].sort(key=key2fn)

    
    def start(self, data=None, broker=None):
        logger.info(f"START data: {data} broker: {broker}")
        self.reconnect(fromstart=True)  # reconnect should be an invariant

        # Datas require some processing to kickstart data reception
        if data is None and broker is None:
            self.cash = None
            return

        # Datas require some processing to kickstart data reception
        if data is not None:
            self._env = data._env
            # For datas simulate a queue with None to kickstart co
            self.datas.append(data)

            # if connection fails, get a fake registration that will force the
            # datas to try to reconnect or else bail out
            return self.getTickerQueue(start=True)

        elif broker is not None:
            self.broker = broker

    
    def stop(self):
        try:
            self.conn.disconnect()  # disconnect should be an invariant
        except AttributeError:
            pass    # conn may have never been connected and lack "disconnect"

        # Unblock any calls set on these events
        self._event_managed_accounts.set()
        self._event_accdownload.set()
    
    # @logibmsg
    def connected(self):
        # The isConnected method is available through __getattr__ indirections
        # and may not be present, which indicates that no connection has been
        # made because the subattribute sender has not yet been created, hence
        # the check for the AttributeError exception
        try:
            return self.conn.isConnected()
        except AttributeError:
            pass

        return False  # non-connected (including non-initialized)

    # @logibmsg
    def reconnect(self, fromstart=False, resub=False):
        # This method must be an invariant in that it can be called several
        # times from the same source and must be consistent. An exampler would
        # be 5 datas which are being received simultaneously and all request a
        # reconnect

        # Policy:
        #  - if dontreconnect has been set, no option to connect is possible
        #  - check connection and use the absence of isConnected as signal of
        #    first ever connection (add 1 to retries too)
        #  - Calculate the retries (forever or not)
        #  - Try to connct
        #  - If achieved and fromstart is false, the datas will be
        #    re-kickstarted to recreate the subscription
        firstconnect = False
        try:
            if self.conn.isConnected():
                if resub:
                    self.startdatas()
                return True  # nothing to do
        except AttributeError:
            # Not connected, several __getattr__ indirections to
            # self.conn.sender.client.isConnected
            firstconnect = True

        if self.dontreconnect:
            return False

        # This is only invoked from the main thread by datas and therefore no
        # lock is needed to control synchronicity to it
        retries = self.p.reconnect
        if retries >= 0:
            retries += firstconnect

        while retries < 0 or retries:
            logger.debug(f"Retries: {retries}")
            if not firstconnect:
                logger.debug(f"Reconnect in {self.p.timeout} secs")
                time.sleep(self.p.timeout)

            firstconnect = False

            try:
                logger.debug("Connect (host={self.p.host}, port={self.p.port}, clientId={self.clientId})")
                if self.conn.connect(self.p.host, self.p.port, self.clientId):
                    if not fromstart or resub:
                        self.startdatas()
                    return True  # connection successful
            except Exception as e:
                logger.exception(f"Failed to Connect {e}")
                return False

            if retries > 0:
                retries -= 1

        self.dontreconnect = True
        return False  # connection/reconnection failed

    
    def startdatas(self):
        # kickstrat datas, not returning until all of them have been done
        ts = list()
        for data in self.datas:
            t = threading.Thread(target=data.reqdata)
            t.start()
            ts.append(t)

        for t in ts:
            t.join()

    @logibmsg
    def stopdatas(self):
        # stop subs and force datas out of the loop (in LIFO order)
        logger.debug(f"Stopping datas")
        qs = list(self.qs.values())
        ts = list()
        for data in self.datas:
            t = threading.Thread(target=data.canceldata)
            t.start()
            ts.append(t)

        for t in ts:
            t.join()

        for q in reversed(qs):  # datamaster the last one to get a None
            q.put(None)

    
    def get_notifications(self):
        '''Return the pending "store" notifications'''
        # The background thread could keep on adding notifications. The None
        # mark allows to identify which is the last notification to deliver
        self.notifs.put(None)  # put a mark
        notifs = list()
        while True:
            notif = self.notifs.get()
            if notif is None:  # mark is reached
                break
            notifs.append(notif)

        return notifs
    
    def error(self, msg):
        # 100-199 Order/Data/Historical related
        # 200-203 tickerId and Order Related
        # 300-399 A mix of things: orders, connectivity, tickers, misc errors
        # 400-449 Seem order related again
        # 500-531 Connectivity/Communication Errors
        # 10000-100027 Mix of special orders/routing
        # 1100-1102 TWS connectivy to the outside
        # 1300- Socket dropped in client-TWS communication
        # 2100-2110 Informative about Data Farm status (id=-1)

        # All errors are logged to the environment (cerebro), because many
        # errors in Interactive Brokers are actually informational and many may
        # actually be of interest to the user
        if msg.reqId > 0:
            logger.error(f"{msg}")
            print(f"Error: {msg}")
        else:
            logger.debug(f"{msg}")

        if msg.reqId == -1 and msg.errorCode == 502:
            print(msg.errorString)

        if not self.p.notifyall:
            self.notifs.put((msg, tuple(vars(msg).values()), dict(vars(msg).items())))

        # Manage those events which have to do with connection
        if msg.errorCode is None:
            # Usually received as an error in connection of just before disconn
            pass
        elif msg.errorCode in [200, 203, 162, 320, 321, 322]:
            # cdetails 200 security not found, notify over right queue
            # cdetails 203 security not allowed for acct
            try:
                q = self.qs[msg.reqId]
            except KeyError:
                pass  # should not happend but it can
            else:
                logger.warn(f"Cancel data queue for {msg.reqId}")
                self.cancelQueue(q, True)

        elif msg.errorCode in [354, 420]:
            # 354 no subscription, 420 no real-time bar for contract
            # the calling data to let the data know ... it cannot resub
            try:
                q = self.qs[msg.reqId]
            except KeyError:
                pass  # should not happend but it can
            else:
                q.put(-msg.errorCode)
                logger.warn(f"Cancel data queue for {msg.reqId}")
                self.cancelQueue(q)

        elif msg.errorCode == 10225:
            # 10225-Bust event occurred, current subscription is deactivated.
            # Please resubscribe real-time bars immediately.
            try:
                q = self.qs[msg.reqId]
            except KeyError:
                pass  # should not happend but it can
            else:
                q.put(-msg.errorCode)

        elif msg.errorCode == 326:  # not recoverable, clientId in use
            self.dontreconnect = True
            self.conn.disconnect()
            self.stopdatas()

        elif msg.errorCode == 502:
            # Cannot connect to TWS: port, config not open, tws off (504 then)
            self.conn.disconnect()
            self.stopdatas()

        elif msg.errorCode == 504:  # Not Connected for data op
            # Once for each data
            # pass  # don't need to manage it

            # Connection lost - Notify ... datas will wait on the queue
            # with no messages arriving
            for q in self.ts:  # key: queue -> ticker
                q.put(-msg.errorCode)

        elif msg.errorCode == 1300:
            # TWS has been closed. The port for a new connection is there
            # newport = int(msg.errorMsg.split('-')[-1])  # bla bla bla -7496
            self.conn.disconnect()
            self.stopdatas()

        elif msg.errorCode == 1100:
            # Connection lost - Notify ... datas will wait on the queue
            # with no messages arriving
            for q in self.ts:  # key: queue -> ticker
                q.put(-msg.errorCode)

        elif msg.errorCode == 1101:
            # Connection restored and tickerIds are gone
            for q in self.ts:  # key: queue -> ticker
                q.put(-msg.errorCode)

        elif msg.errorCode == 1102:
            # Connection restored and tickerIds maintained
            for q in self.ts:  # key: queue -> ticker
                q.put(-msg.errorCode)

        elif msg.errorCode < 500:
            # Given the myriad of errorCodes, start by assuming is an order
            # error and if not, the checks there will let it go
            if msg.reqId < self.REQIDBASE:
                if self.broker is not None:
                    self.broker.push_ordererror(msg)
            else:
                # Cancel the queue if a "data" reqId error is given: sanity
                q = self.qs[msg.reqId]
                logger.warn(f"Cancel data queue for {msg.reqId}")
                self.cancelQueue(q, True)

    
    def connectionClosed(self):
        # Sometmes this comes without 1300/502 or any other and will not be
        # seen in error hence the need to manage the situation independently
        if self.connected():
            self.conn.disconnect()
            self.stopdatas()
    
    def updateAccountTime(self, timeStamp):
        logger.debug(f"timeStamp: {timeStamp}")

    def connectAck(self):
        logger.debug(f"connectAck")
    
    def managedAccounts(self, accountsList):
        # 1st message in the stream
        self.managed_accounts = accountsList.split(',')
        self._event_managed_accounts.set()

        # Request time to avoid synchronization issues
        self.reqCurrentTime()

    @logibmsg
    def reqCurrentTime(self):
        self.conn.reqCurrentTime()
    
    def currentTime(self, time):
        if not self.p.timeoffset:  # only if requested ... apply timeoffset
            return
        curtime = datetime.fromtimestamp(float(time))
        with self._lock_tmoffset:
            self.tmoffset = curtime - datetime.now()

        threading.Timer(self.p.timerefresh, self.reqCurrentTime).start()
    
    def timeoffset(self):
        with self._lock_tmoffset:
            return self.tmoffset

    def nextTickerId(self):
        # Get the next ticker using next on the itertools.count
        return next(self._tickerId)

    def nextValidId(self, orderId):
        # Create a counter from the TWS notified value to apply to orders
        self.orderid = itertools.count(orderId)

    def nextOrderId(self):
        # Get the next ticker using next on the itertools.count made with the
        # notified value from TWS
        return next(self.orderid)

    def reuseQueue(self, tickerId):
        '''Reuses queue for tickerId, returning the new tickerId and q'''
        with self._lock_q:
            # Invalidate tickerId in qs (where it is a key)
            q = self.qs.pop(tickerId, None)  # invalidate old
            iscash = self.iscash.pop(tickerId, None)

            # Update ts: q -> ticker
            tickerId = self.nextTickerId()  # get new tickerId
            self.ts[q] = tickerId  # Update ts: q -> tickerId
            self.qs[tickerId] = q  # Update qs: tickerId -> q
            self.iscash[tickerId] = iscash

        return tickerId, q
    
    def getTickerQueue(self, start=False):
        '''Creates ticker/Queue for data delivery to a data feed'''
        q = queue.Queue()
        if start:
            q.put(None)
            return q

        with self._lock_q:
            tickerId = self.nextTickerId()
            self.qs[tickerId] = q  # can be managed from other thread
            self.ts[q] = tickerId
            self.iscash[tickerId] = False

        return tickerId, q
    
    def cancelQueue(self, q, sendnone=False):
        '''Cancels a Queue for data delivery'''
        # pop ts (tickers) and with the result qs (queues)
        tickerId = self.ts.pop(q, None)
        self.qs.pop(tickerId, None)

        self.iscash.pop(tickerId, None)

        if sendnone:
            q.put(None)
    
    def validQueue(self, q):
        '''Returns (bool)  if a queue is still valid'''
        return q in self.ts  # queue -> ticker
    
    def getContractDetails(self, contract, maxcount=None):
        cds = list()
        q = self.reqContractDetails(contract)
        while True:
            msg = q.get()
            if msg is None:
                break
            cds.append(msg)

        if not cds or (maxcount and len(cds) > maxcount):
            err = 'Ambiguous contract: none/multiple answers received'
            self.notifs.put((err, cds, {}))
            return None

        return cds
    
    def reqContractDetails(self, contract):
        # get a ticker/queue for identification/data delivery
        tickerId, q = self.getTickerQueue()
        self.conn.reqContractDetails(tickerId, contract)
        return q

    def contractDetailsEnd(self, reqId):
        '''Signal end of contractdetails'''
        logger.debug(f"Cancel data queue tickerId: {reqId} Q: {self.qs[reqId]}")
        self.cancelQueue(self.qs[reqId], True)
        
    def contractDetails(self, reqId, contractDetails):
        '''Receive answer and pass it to the queue'''
        self.qs[reqId].put(contractDetails)

    @logibmsg
    def reqHistoricalDataEx(self, contract, enddate, begindate,
                            timeframe, compression,
                            what=None, useRTH=False, tz='', sessionend=None,
                            tickerId=None):
        '''
        Extension of the raw reqHistoricalData proxy, which takes two dates
        rather than a duration, barsize and date

        It uses the IB published valid duration/barsizes to make a mapping and
        spread a historical request over several historical requests if needed
        '''
        # Keep a copy for error reporting purposes
        kwargs = locals().copy()
        kwargs.pop('self', None)  # remove self, no need to report it

        if timeframe < TimeFrame.Seconds:
            # Ticks are not supported
            return self.getTickerQueue(start=True)

        if enddate is None:
            enddate = datetime.now()

        if begindate is None:
            duration = self.getmaxduration(timeframe, compression)
            if duration is None:
                err = ('No duration for historical data request for '
                       'timeframe/compresison')
                self.notifs.put((err, (), kwargs))
                return self.getTickerQueue(start=True)
            barsize = self.tfcomp_to_size(timeframe, compression)
            if barsize is None:
                err = ('No supported barsize for historical data request for '
                       'timeframe/compresison')
                self.notifs.put((err, (), kwargs))
                return self.getTickerQueue(start=True)

            return self.reqHistoricalData(contract=contract, enddate=enddate,
                                          duration=duration, barsize=barsize,
                                          what=what, useRTH=useRTH, tz=tz,
                                          sessionend=sessionend)

        # Check if the requested timeframe/compression is supported by IB
        durations = self.getdurations(timeframe, compression)
        if not durations:  # return a queue and put a None in it
            return self.getTickerQueue(start=True)

        # Get or reuse a queue
        if tickerId is None:
            tickerId, q = self.getTickerQueue()
            logger.debug(f"Get tickerId: {tickerId} Q: {q}")
        else:
            tickerId, q = self.reuseQueue(tickerId)  # reuse q for old tickerId
            logger.debug(f"Reuse tickerId: {tickerId} Q: {q}")

        # Get the best possible duration to reduce number of requests
        duration = None
        for dur in durations:
            intdate = self.dt_plus_duration(begindate, dur)
            if intdate >= enddate:
                intdate = enddate
                duration = dur  # begin -> end fits in single request
                break

        if duration is None:  # no duration large enough to fit the request
            duration = durations[-1]

            # Store the calculated data
            self.histexreq[tickerId] = dict(
                contract=contract, enddate=enddate, begindate=intdate,
                timeframe=timeframe, compression=compression,
                what=what, useRTH=useRTH, tz=tz, sessionend=sessionend)

        barsize = self.tfcomp_to_size(timeframe, compression)
        self.histfmt[tickerId] = timeframe >= TimeFrame.Days
        self.histsend[tickerId] = sessionend
        self.histtz[tickerId] = tz

        if contract.secType in ['CASH', 'CFD']:
            self.iscash[tickerId] = 1  # msg.field code
            if not what:
                what = 'BID'  # default for cash unless otherwise specified

        elif contract.secType in ['IND'] and self.p.indcash:
            self.iscash[tickerId] = 4  # msg.field code

        what = what or 'TRADES'

        self.conn.reqHistoricalData(
            tickerId,
            contract,
            #bytes(intdate.strftime('%Y%m%d %H:%M:%S') + ' GMT'),
            bytes(intdate.strftime('%Y%m%d-%H:%M:%S')),
            bytes(duration),
            bytes(barsize),
            bytes(what),
            int(useRTH),
            2, # dateformat 1 for string, 2 for unix time in seconds
            False,
            [])

        return q
    
    def reqHistoricalData(self, contract, enddate, duration, barsize,
                          what=None, useRTH=False, tz='', sessionend=None):
        '''Proxy to reqHistorical Data'''

        # get a ticker/queue for identification/data delivery
        tickerId, q = self.getTickerQueue()

        if contract.secType in ['CASH', 'CFD']:
            self.iscash[tickerId] = True
            if not what:
                what = 'BID'  # TRADES doesn't work
            elif what == 'ASK':
                self.iscash[tickerId] = 2
        else:
            what = what or 'TRADES'

        # split barsize "x time", look in sizes for (tf, comp) get tf
        tframe = self._sizes[barsize.split()[1]][0]
        self.histfmt[tickerId] = tframe >= TimeFrame.Days
        self.histsend[tickerId] = sessionend
        self.histtz[tickerId] = tz

        self.conn.reqHistoricalData(
            tickerId,
            contract,
            # bytes(enddate.strftime('%Y%m%d %H:%M:%S') + ' GMT'),
            bytes(enddate.strftime('%Y%m%d-%H:%M:%S')),
            bytes(duration),
            bytes(barsize),
            bytes(what),
            int(useRTH),
            2,
            False,
            [])

        return q

    def reqHistoricalTicksEx(self, contract, enddate=None, begindate=None,
                            what=None, useRTH=False, tz='',
                            tickerId=None):
        '''
        Extension of the raw reqHistoricalData proxy, which takes two dates
        rather than a duration, barsize and date

        It uses the IB published valid duration/barsizes to make a mapping and
        spread a historical request over several historical requests if needed
        '''
        # Keep a copy for error reporting purposes
        kwargs = locals().copy()
        kwargs.pop('self', None)  # remove self, no need to report it

        if enddate and begindate:
            err = ('Only fromdate OR enddate can be specified not both')
            self.notifs.put((err, (), kwargs))
            return self.getTickerQueue(start=True)

        if enddate is None and begindate is None:
            today = datetime.utcnow().date()
            begindate = datetime(today.year, today.month, today.day)
            # begindate = datetime.now()

        logger.debug(f"begin: {begindate} end: {enddate}")

        # Get or reuse a queue
        if tickerId is None:
            tickerId, q = self.getTickerQueue()
            logger.debug(f"Get tickerId: {tickerId} Q: {q}")
        else:
            tickerId, q = self.reuseQueue(tickerId)  # reuse q for old tickerId
            logger.debug(f"Reuse tickerId: {tickerId} Q: {q}")

        if contract.secType in ['CASH', 'CFD']:
            self.iscash[tickerId] = 1  # msg.field code
            if not what:
                what = 'BID'  # default for cash unless otherwise specified

        elif contract.secType in ['IND'] and self.p.indcash:
            self.iscash[tickerId] = 4  # msg.field code

        what = what or 'TRADES'

        self.conn.reqHistoricalTicks(
            tickerId,
            contract,
            # bytes(begindate.strftime('%Y%m%d %H:%M:%S') + ' GMT') if begindate else '',
            # bytes(enddate.strftime('%Y%m%d %H:%M:%S') + ' GMT') if enddate else '',
            bytes(begindate.strftime('%Y%m%d-%H:%M:%S')) if begindate else '',
            bytes(enddate.strftime('%Y%m%d-%H:%M:%S')) if enddate else '',
            100,
            bytes(what),
            int(useRTH),
            True,
            [])

        return q

    def reqHistoricalTicks(self, contract, enddate, begindate,
                          what=None, useRTH=False, tz=''):
        '''Proxy to reqHistoricalTicks'''

        # get a ticker/queue for identification/data delivery
        tickerId, q = self.getTickerQueue()

        if contract.secType in ['CASH', 'CFD']:
            self.iscash[tickerId] = True
            if not what:
                what = 'BID'  # TRADES doesn't work
            elif what == 'ASK':
                self.iscash[tickerId] = 2
        else:
            what = what or 'TRADES'

        if what == 'TRADES_ALL':
            what = 'LastAll'
        elif what == 'TRADES':
            what = 'Last'
        elif what == 'BID_ASK':
            when = 'BidAsk'
        elif what == 'MIDPOINT':
            when = 'MidPoint'

        self.conn.reqHistoricalTicks(
            tickerId,
            contract,
            # bytes(begindate.strftime('%Y%m%d %H:%M:%S') + ' GMT') if begindate else '',
            bytes(begindate.strftime('%Y%m%d-%H:%M:%S')) if begindate else '',
            # bytes(enddate.strftime('%Y%m%d %H:%M:%S') + ' GMT'),
            # bytes(enddate.strftime('%Y%m%d %H:%M:%S') + ' GMT') if enddate else '',
            bytes(enddate.strftime('%Y%m%d-%H:%M:%S')) if enddate else '',
            10,
            bytes(what),
            # int(useRTH),
            1,
            True,
            [])

        return q
    
    def cancelHistoricalData(self, q):
        '''Cancels an existing HistoricalData request

        Params:
          - q: the Queue returned by reqMktData
        '''
        with self._lock_q:
            self.conn.cancelHistoricalData(self.ts[q])
            logger.warn(f"Cancel data queue for {q}")
            self.cancelQueue(q, True)

    @logibmsg
    def reqRealTimeBars(self, contract, useRTH=False, duration=5, what = None):
        '''Creates a request for (5 seconds) Real Time Bars

        Params:
          - contract: a ib.ext.Contract.Contract intance
          - useRTH: (default: False) passed to TWS
          - duration: (default: 5) passed to TWS, no other value works in 2016)

        Returns:
          - a Queue the client can wait on to receive a RTVolume instance
        '''
        # get a ticker/queue for identification/data delivery
        tickerId, q = self.getTickerQueue()

        what = what or 'TRADES'

        # 20150929 - Only 5 secs supported for duration
        self.conn.reqRealTimeBars(
            tickerId,
            contract,
            duration,
            # bytes('TRADES'),
            bytes(what),
            useRTH,
            [])

        return q

    def cancelRealTimeBars(self, q):
        '''Cancels an existing MarketData subscription

        Params:
          - q: the Queue returned by reqMktData
        '''
        with self._lock_q:
            tickerId = self.ts.get(q, None)
            if tickerId is not None:
                self.conn.cancelRealTimeBars(tickerId)

            logger.debug(f"Cancel data queue for {tickerId}")
            self.cancelQueue(q, True)

    def reqMktData(self, contract, what=None):
        '''Creates a MarketData subscription

        Params:
          - contract: a ib.ext.Contract.Contract intance

        Returns:
          - a Queue the client can wait on to receive a RTVolume instance
        '''
        # get a ticker/queue for identification/data delivery
        tickerId, q = self.getTickerQueue()
        ticks = '233'  # request RTVOLUME tick delivered over tickString

        if contract.secType in ['CASH', 'CFD']:
            self.iscash[tickerId] = True
            ticks = ''  # cash markets do not get RTVOLUME
            if what == 'ASK':
                self.iscash[tickerId] = 2

        # q.put(None)  # to kickstart backfilling
        # Can request 233 also for cash ... nothing will arrive
        self.conn.reqMktData(tickerId, contract, bytes(ticks), False, False, [])
        return q

    def reqTickByTickData(self, contract, what=None, ignoreSize=True):
        '''
        Tick-by-tick data corresponding to the data shown in the 
        TWS Time & Sales Window is available starting with TWS v969 and API v973.04.
        '''    

        if what == 'TRADES':
            what = 'Last'
        elif what == 'TRADES_ALL':
            what = 'AllLast'
        elif what == 'BID_ASK':
            what = 'BidAsk'
        elif what == 'MIDPOINT':
            what = 'MidPoint'
        else:
            what = 'Last'

        tickerId, q = self.getTickerQueue()
        self.conn.reqTickByTickData(tickerId, contract, what, 0, ignoreSize)
        return q
    
    def cancelMktData(self, q):
        '''Cancels an existing MarketData subscription

        Params:
          - q: the Queue returned by reqMktData
        '''
        with self._lock_q:
            tickerId = self.ts.get(q, None)
            if tickerId is not None:
                self.conn.cancelMktData(tickerId)

            logger.debug(f"Cancel data queue for {tickerId}")
            self.cancelQueue(q, True)

    def cancelTickByTickData(self, q):
        '''Cancels an existing MarketData subscription

        Params:
          - q: the Queue returned by reqTickByTickData
        '''
        with self._lock_q:
            tickerId = self.ts.get(q, None)
            if tickerId is not None:
                self.conn.cancelTickByTickData(tickerId)

            logger.debug(f"Cancel data queue for {tickerId}")
            self.cancelQueue(q, True)
    
    def tickString(self, reqId, tickType, value):
        # Receive and process a tickString message
        tickerId = reqId
        if tickType == 48:  # RTVolume
            try:
                rtvol = RTVolume(value)
            except ValueError:  # price not in message ...
                pass
            else:
                # Don't need to adjust the time, because it is in "timestamp"
                # form in the message
                self.qs[tickerId].put(rtvol)

    def tickPrice(self, reqId, tickType, price, attrib):
        '''Cash Markets have no notion of "last_price"/"last_size" and the
        tracking of the price is done (industry de-facto standard at least with
        the IB API) following the BID price

        A RTVolume which will only contain a price is put into the client's
        queue to have a consistent cross-market interface
        '''

        # Used for "CASH" markets
        # The price field has been seen to be missing in some instances even if
        # "field" is 1
        tickerId = reqId
        fieldcode = self.iscash[tickerId]
        if fieldcode:
            if tickType == fieldcode:  # Expected cash field code
                try:
                    if price == -1.0:
                        # seems to indicate the stream is halted for example in
                        # between 23:00 - 23:15 CET for FOREX
                        return
                except AttributeError:
                    pass

                try:
                    rtvol = RTVolume(price=price, tmoffset=self.tmoffset)
                    # print('rtvol with datetime:', rtvol.datetime)
                except ValueError:  # price not in message ...
                    pass
                else:
                    self.qs[tickerId].put(rtvol)
        else:
            # Non-cash
            try:
                if price == -1.0:
                    # seems to indicate the stream is halted for example in
                    # between 23:00 - 23:15 CET for FOREX
                    return
            except AttributeError:
                pass
            rtprice = RTPrice(price=price, tmoffset=self.tmoffset)
            self.qs[tickerId].put(rtprice)

    def tickSize(self, reqId, tickType, size):
        tickerId = reqId
        rtsize = RTSize(size=size, tmoffset=self.tmoffset)
        self.qs[tickerId].put(rtsize)

    def tickGeneric(self, reqId, tickType, value):
        try:
            if value == -1.0:
                # seems to indicate the stream is halted for example in
                # between 23:00 - 23:15 CET for FOREX
                return
        except AttributeError:
            pass
        tickerId = reqId
        value = value # if msg.value != 0.0 else (1.0 + random.random())
        rtprice = RTPrice(price=value, tmoffset=self.tmoffset)
        self.qs[tickerId].put(rtprice)

    def realtimeBar(self, msg):
        '''Receives x seconds Real Time Bars (at the time of writing only 5
        seconds are supported)

        Not valid for cash markets
        '''
        # Get a naive localtime object
        msg.time = datetime.utcfromtimestamp(float(msg.time))
        self.qs[msg.reqId].put(msg)

    def historicalData(self, msg):
        '''Receives the events of a historical data request'''
        # For multi-tiered downloads we'd need to rebind the queue to a new
        # tickerId (in case tickerIds are not reusable) and instead of putting
        # None, issue a new reqHistData with the new data and move formward
        tickerId = msg.reqId
        q = self.qs[tickerId]

        dtstr = msg.date  # Format when string req: YYYYMMDD[  HH:MM:SS]
        if self.histfmt[tickerId]:
            sessionend = self.histsend[tickerId]
            dt = datetime.strptime(dtstr, '%Y%m%d')
            dteos = datetime.combine(dt, sessionend)
            tz = self.histtz[tickerId]
            if tz:
                dteostz = tz.localize(dteos)
                dteosutc = dteostz.astimezone(UTC).replace(tzinfo=None)
                # When requesting for example daily bars, the current day
                # will be returned with the already happened data. If the
                # session end were added, the new ticks wouldn't make it
                # through, because they happen before the end of time
            else:
                dteosutc = dteos

            if dteosutc <= datetime.utcnow():
                dt = dteosutc

            msg.date = dt
        else:
            msg.date = datetime.utcfromtimestamp(long(dtstr))

        q.put(msg)
    
    def historicalDataEnd(self, reqId, start, end):
        tickerId = reqId
        self.histfmt.pop(tickerId, None)
        self.histsend.pop(tickerId, None)
        self.histtz.pop(tickerId, None)
        kargs = self.histexreq.pop(tickerId, None)
        if kargs is not None:
            self.reqHistoricalDataEx(tickerId=tickerId, **kargs)
            return

        q = self.qs[tickerId]
        self.cancelQueue(q)

    def historicalTicks(self, reqId, tick):
        tickerId = reqId
        self.qs[tickerId].put(tick)

    def historicalTicksEnd(self, reqId):
        tickerId = reqId
        q = self.qs[tickerId]
        self.cancelTickByTickData(q)

    def tickByTickBidAsk(self, reqId, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):
        tickerId = reqId
        tick = RTTickBidAsk(time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk)
        self.qs[tickerId].put(tick)

    def tickByTickAllLast(self, reqId, tickType, time, price, size, tickAtrribLast, exchange, specialConditions):
        tickerId = reqId
        tick = RTTickLast(tickType, time, price, size, tickAtrribLast, exchange, specialConditions)
        self.qs[tickerId].put(tick)

    def tickByTickMidPoint(self, reqId, time, midPoint):
        tickerId = reqId
        tick = RTTickMidPoint(time, time, midPoint)
        self.qs[tickerId].put(tick)

    # The _durations are meant to calculate the needed historical data to
    # perform backfilling at the start of a connetion or a connection is lost.
    # Using a timedelta as a key allows to quickly find out which bar size
    # bar size (values in the tuples int the dict) can be used.

    _durations = dict([
        # 60 seconds - 1 min
        ('60 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min')),

        # 120 seconds - 2 mins
        ('120 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins')),

        # 180 seconds - 3 mins
        ('180 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins')),

        # 300 seconds - 5 mins
        ('300 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins')),

        # 600 seconds - 10 mins
        ('600 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins')),

        # 900 seconds - 15 mins
        ('900 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins')),

        # 1200 seconds - 20 mins
        ('1200 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins')),

        # 1800 seconds - 30 mins
        ('1800 S',
         ('1 secs', '5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins')),

        # 3600 seconds - 1 hour
        ('3600 S',
         ('5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour')),

        # 7200 seconds - 2 hours
        ('7200 S',
         ('5 secs', '10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours')),

        # 10800 seconds - 3 hours
        ('10800 S',
         ('10 secs', '15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours')),

        # 14400 seconds - 4 hours
        ('14400 S',
         ('15 secs', '30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours')),

        # 28800 seconds - 8 hours
        ('28800 S',
         ('30 secs',
          '1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours', '8 hours')),

        # 1 days
        ('1 D',
         ('1 min', '2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
          '1 day')),

        # 2 days
        ('2 D',
         ('2 mins', '3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
          '1 day')),

        # 1 weeks
        ('1 W',
         ('3 mins', '5 mins', '10 mins', '15 mins',
          '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
          '1 day', '1 W')),

        # 2 weeks
        ('2 W',
         ('15 mins', '20 mins', '30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
          '1 day', '1 W')),

        # 1 months
        ('1 M',
         ('30 mins',
          '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
          '1 day', '1 W', '1 M')),

        # 2+ months
        ('2 M', ('1 day', '1 W', '1 M')),
        ('3 M', ('1 day', '1 W', '1 M')),
        ('4 M', ('1 day', '1 W', '1 M')),
        ('5 M', ('1 day', '1 W', '1 M')),
        ('6 M', ('1 day', '1 W', '1 M')),
        ('7 M', ('1 day', '1 W', '1 M')),
        ('8 M', ('1 day', '1 W', '1 M')),
        ('9 M', ('1 day', '1 W', '1 M')),
        ('10 M', ('1 day', '1 W', '1 M')),
        ('11 M', ('1 day', '1 W', '1 M')),

        # 1+ years
        ('1 Y',  ('1 day', '1 W', '1 M')),
    ])

    # Sizes allow for quick translation from bar sizes above to actual
    # timeframes to make a comparison with the actual data
    _sizes = {
        'secs': (TimeFrame.Seconds, 1),
        'min': (TimeFrame.Minutes, 1),
        'mins': (TimeFrame.Minutes, 1),
        'hour': (TimeFrame.Minutes, 60),
        'hours': (TimeFrame.Minutes, 60),
        'day': (TimeFrame.Days, 1),
        'W': (TimeFrame.Weeks, 1),
        'M': (TimeFrame.Months, 1),
    }

    _dur2tf = {
        'S': TimeFrame.Seconds,
        'D': TimeFrame.Days,
        'W': TimeFrame.Weeks,
        'M': TimeFrame.Months,
        'Y': TimeFrame.Years,
    }

    def getdurations(self,  timeframe, compression):
        key = (timeframe, compression)
        if key not in self.revdur:
            return []

        return self.revdur[key]

    def getmaxduration(self, timeframe, compression):
        key = (timeframe, compression)
        try:
            return self.revdur[key][-1]
        except (KeyError, IndexError):
            pass

        return None

    def tfcomp_to_size(self, timeframe, compression):
        if timeframe == TimeFrame.Months:
            return '{} M'.format(compression)

        if timeframe == TimeFrame.Weeks:
            return '{} W'.format(compression)

        if timeframe == TimeFrame.Days:
            if not compression % 7:
                return '{} W'.format(compression // 7)

            return '{} day'.format(compression)

        if timeframe == TimeFrame.Minutes:
            if not compression % 60:
                hours = compression // 60
                return ('{} hour'.format(hours)) + ('s' * (hours > 1))

            return ('{} min'.format(compression)) + ('s' * (compression > 1))

        if timeframe == TimeFrame.Seconds:
            return '{} secs'.format(compression)

        # Microseconds or ticks
        return None

    def dt_plus_duration(self, dt, duration):
        size, dim = duration.split()
        size = int(size)
        if dim == 'S':
            return dt + timedelta(seconds=size)

        if dim == 'D':
            return dt + timedelta(days=size)

        if dim == 'W':
            return dt + timedelta(days=size * 7)

        if dim == 'M':
            month = dt.month - 1 + size  # -1 to make it 0 based, readd below
            years, month = divmod(month, 12)
            return dt.replace(year=dt.year + years, month=month + 1)

        if dim == 'Y':
            return dt.replace(year=dt.year + size)

        return dt  # could do nothing with it ... return it intact

    def calcdurations(self, dtbegin, dtend):
        '''Calculate a duration in between 2 datetimes'''
        duration = self.histduration(dtbegin, dtend)

        if duration[-1] == 'M':
            m = int(duration.split()[0])
            m1 = min(2, m)  # (2, 1) -> 1, (2, 7) -> 2. Bottomline: 1 or 2
            m2 = max(1, m1)  # m1 can only be 1 or 2
            checkdur = '{} M'.format(m2)
        elif duration[-1] == 'Y':
            checkdur = '1 Y'
        else:
            checkdur = duration

        sizes = self._durations[checkdur]
        return duration, sizes

    def calcduration(self, dtbegin, dtend):
        '''Calculate a duration in between 2 datetimes. Returns single size'''
        duration, sizes = self._calcdurations(dtbegin, dtend)
        return duration, sizes[0]

    
    def histduration(self, dt1, dt2):
        # Given two dates calculates the smallest possible duration according
        # to the table from the Historical Data API limitations provided by IB
        #
        # Seconds: 'x S' (x: [60, 120, 180, 300, 600, 900, 1200, 1800, 3600,
        #                     7200, 10800, 14400, 28800])
        # Days: 'x D' (x: [1, 2]
        # Weeks: 'x W' (x: [1, 2])
        # Months: 'x M' (x: [1, 11])
        # Years: 'x Y' (x: [1])

        td = dt2 - dt1  # get a timedelta for calculations

        # First: array of secs
        tsecs = td.total_seconds()
        secs = [60, 120, 180, 300, 600, 900, 1200, 1800, 3600, 7200, 10800,
                14400, 28800]

        idxsec = bisect.bisect_left(secs, tsecs)
        if idxsec < len(secs):
            return '{} S'.format(secs[idxsec])

        tdextra = bool(td.seconds or td.microseconds)  # over days/weeks

        # Next: 1 or 2 days
        days = td.days + tdextra
        if td.days <= 2:
            return '{} D'.format(days)

        # Next: 1 or 2 weeks
        weeks, d = divmod(td.days, 7)
        weeks += bool(d or tdextra)
        if weeks <= 2:
            return '{} W'.format(weeks)

        # Get references to dt components
        y2, m2, d2 = dt2.year, dt2.month, dt2.day
        y1, m1, d1 = dt1.year, dt1.month, dt2.day

        H2, M2, S2, US2 = dt2.hour, dt2.minute, dt2.second, dt2.microsecond
        H1, M1, S1, US1 = dt1.hour, dt1.minute, dt1.second, dt1.microsecond

        # Next: 1 -> 11 months (11 incl)
        months = (y2 * 12 + m2) - (y1 * 12 + m1) + (
            (d2, H2, M2, S2, US2) > (d1, H1, M1, S1, US1))
        if months <= 1:  # months <= 11
            return '1 M'  # return '{} M'.format(months)
        elif months <= 11:
            return '2 M'  # cap at 2 months to keep the table clean

        # Next: years
        # y = y2 - y1 + (m2, d2, H2, M2, S2, US2) > (m1, d1, H1, M1, S1, US1)
        # return '{} Y'.format(y)

        return '1 Y'  # to keep the table clean

    def makecontract(self, symbol, sectype, exch, curr,
                     expiry='', strike=0.0, right='', mult=1, 
                     primaryExch=None, localSymbol=None):
        '''returns a contract from the parameters without check'''

        contract = Contract()
        
        if localSymbol:
            contract.localSymbol = bytes(localSymbol)
        else:
            contract.symbol = bytes(symbol)
        
        contract.secType = bytes(sectype)
        contract.exchange = bytes(exch)
        if primaryExch:
            contract.primaryExchange = bytes(primaryExch)
        if curr:
            contract.currency = bytes(curr)
        if sectype in ['FUT', 'OPT', 'FOP']:
            contract.lastTradeDateOrContractMonth = bytes(expiry)
        if sectype in ['OPT', 'FOP']:
            contract.strike = strike
            contract.right = bytes(right)
        if mult:
            contract.multiplier = bytes(mult)
        return contract

    def cancelOrder(self, orderid):
        '''Proxy to cancelOrder'''
        self.conn.cancelOrder(orderid)
    
    def placeOrder(self, orderid, contract, order):
        '''Proxy to placeOrder'''
        self.conn.placeOrder(orderid, contract, order)
    
    def openOrder(self, msg):
        '''Receive the event ``openOrder`` events'''
        self.broker.push_orderstate(msg)
    
    def openOrderEnd(self):
        # TODO: Add event to manage order requests 
        logger.debug(f"openOrderEnd")
    
    def execDetails(self, reqId, contract, execution):
        '''Receive execDetails'''
        execution.shares = float(execution.shares)
        execution.cumQty = float(execution.cumQty)
        self.broker.push_execution(execution)
    
    def orderStatus(self, msg):
        '''Receive the event ``orderStatus``'''
        self.broker.push_orderstatus(msg)
    
    def commissionReport(self, commissionReport):
        '''Receive the event commissionReport'''
        self.broker.push_commissionreport(commissionReport)

    def reqPositions(self):
        '''Proxy to reqPositions'''
        self.conn.reqPositions()
        
    def position(self, account, contract, pos, avgCost):
        '''Receive event positions'''
        # Lock access to the position dicts. This is called in sub-thread and
        # can kick in at any time
        with self._lock_pos:
            try:
                if not self._event_accdownload.is_set():  # 1st event seen
                    position = Position(float(pos), float(avgCost))
                    logger.debug(f"POSITIONS INITIAL: {self.positions}")
                    self.positions[contract.conId] = position
                else:
                    position = self.positions[contract.conId]
                    logger.debug(f"POSITION UPDATE: {position}")
                    if not position.fix(float(pos), avgCost):
                        err = ('The current calculated position and '
                            'the position reported by the broker do not match. '
                            'Operation can continue, but the trades '
                            'calculated in the strategy may be wrong')

                        self.notifs.put((err, (), {}))

                    # self.broker.push_portupdate()
            except Exception as e:
                logger.exception(f"Exception: {e}")

    def positionEnd(self):
        logger.debug(f"positionEnd")

    def reqAccountUpdates(self, subscribe=True, account=None):
        '''Proxy to reqAccountUpdates

        If ``account`` is ``None``, wait for the ``managedAccounts`` message to
        set the account codes
        '''
        if account is None:
            self._event_managed_accounts.wait()
            account = self.managed_accounts[0]

        self.conn.reqAccountUpdates(subscribe, bytes(account))

    def accountDownloadEnd(self, accountName):
        # Signals the end of an account update
        # the event indicates it's over. It's only false once, and can be used
        # to find out if it has at least been downloaded once
        self._event_accdownload.set()
        if False:
            if self.port_update:
                self.broker.push_portupdate()

                self.port_update = False

    def updatePortfolio(self, contract, pos,
                        marketPrice, marketValue,
                        averageCost, unrealizedPNL,
                        realizedPNL, accountName):
        # Lock access to the position dicts. This is called in sub-thread and
        # can kick in at any time
        with self._lock_pos:
            try:
                if not self._event_accdownload.is_set():  # 1st event seen
                    position = Position(float(pos), float(averageCost))
                    logger.debug(f"POSITIONS INITIAL: {self.positions}")
                    # self.positions[contract.conId] = position
                    self.positions.setdefault(contract.conId, position)
                else:
                    position = self.positions[contract.conId]
                    logger.debug(f"POSITION UPDATE: {position}")
                    if not position.fix(float(pos), averageCost):
                        err = ('The current calculated position and '
                            'the position reported by the broker do not match. '
                            'Operation can continue, but the trades '
                            'calculated in the strategy may be wrong')

                        self.notifs.put((err, (), {}))

                    # Flag signal to broker at the end of account download
                    # self.port_update = True
                    self.broker.push_portupdate()
            except Exception as e:
                logger.exception(f"Exception: {e}")

    def getposition(self, contract, clone=False):
        # Lock access to the position dicts. This is called from main thread
        # and updates could be happening in the background
        with self._lock_pos:
            position = self.positions[contract.conId]
            if clone:
                return copy(position)

            return position

    @logibmsg
    def updateAccountValue(self, key, value, currency, accountName):
        # Lock access to the dicts where values are updated. This happens in a
        # sub-thread and could kick it at anytime
        with self._lock_accupd:
            try:
                value = float(value)
            except ValueError:
                value = value

            self.acc_upds[accountName][key][currency] = value

            if key == 'NetLiquidation':
                # NetLiquidationByCurrency and currency == 'BASE' is the same
                self.acc_value[accountName] = value
            elif key == 'CashBalance' and currency == 'BASE':
                self.acc_cash[accountName] = value
    
    @logibmsg
    def get_acc_values(self, account=None):
        '''Returns all account value infos sent by TWS during regular updates
        Waits for at least 1 successful download

        If ``account`` is ``None`` then a dictionary with accounts as keys will
        be returned containing all accounts

        If account is specified or the system has only 1 account the dictionary
        corresponding to that account is returned
        '''
        # Wait for at least 1 account update download to have been finished
        # before the account infos can be returned to the calling client
        # if self.connected():
        #     self._event_accdownload.wait()
        # Lock access to acc_cash to avoid an event intefering
        with self._updacclock:
            if account is None:
                # wait for the managedAccount Messages
                # if self.connected():
                #     self._event_managed_accounts.wait()

                if not self.managed_accounts:
                    return self.acc_upds.copy()

                elif len(self.managed_accounts) > 1:
                    return self.acc_upds.copy()

                # Only 1 account, fall through to return only 1
                account = self.managed_accounts[0]

            try:
                return self.acc_upds[account].copy()
            except KeyError:
                pass

            return self.acc_upds.copy()

    @logibmsg
    def get_acc_value(self, account=None):
        '''Returns the net liquidation value sent by TWS during regular updates
        Waits for at least 1 successful download

        If ``account`` is ``None`` then a dictionary with accounts as keys will
        be returned containing all accounts

        If account is specified or the system has only 1 account the dictionary
        corresponding to that account is returned
        '''
        # Wait for at least 1 account update download to have been finished
        # before the value can be returned to the calling client
        # Lock access to acc_cash to avoid an event intefering

        with self._updacclock:
            if account is None:
                if not self.managed_accounts:
                    return float()
                elif len(self.managed_accounts) > 1:
                    return sum(self.acc_value.values())

                # Only 1 account, fall through to return only 1
                account = self.managed_accounts[0]
                
            try:
                return self.acc_value[account]
            except KeyError:
                pass

        return float()

    @logibmsg
    def get_acc_cash(self, account=None):
        '''Returns the total cash value sent by TWS during regular updates
        Waits for at least 1 successful download

        If ``account`` is ``None`` then a dictionary with accounts as keys will
        be returned containing all accounts

        If account is specified or the system has only 1 account the dictionary
        corresponding to that account is returned
        '''
        # Wait for at least 1 account update download to have been finished
        # before the cash can be returned to the calling client
        # if self.connected():
        #     self._event_accdownload.wait()
        # Lock access to acc_cash to avoid an event intefering
        with self._lock_accupd:
            if account is None:
                # # wait for the managedAccount Messages
                # if self.connected():
                #     self._event_managed_accounts.wait()

                if not self.managed_accounts:
                    return float()

                elif len(self.managed_accounts) > 1:
                    return sum(self.acc_cash.values())

                # Only 1 account, fall through to return only 1
                account = self.managed_accounts[0]

            try:
                return self.acc_cash[account]
            except KeyError:
                pass
