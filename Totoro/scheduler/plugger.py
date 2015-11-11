#!/usr/bin/env python
# encoding: utf-8
"""
pluger.py

Created by José Sánchez-Gallego on 20 Oct 2014.
Licensed under a 3-clause BSD license.

Revision history:
    20 Oct 2014 J. Sánchez-Gallego
      Initial version
    15 Nov 2014 J. Sánchez-Gallego
      Improved the logic and added some convenience functions

"""

from __future__ import division
from __future__ import print_function
from Totoro import log, config, site
from Totoro.db import getConnection
from Totoro.scheduler.timeline import Timeline
from Totoro import exceptions
from Totoro.utils import intervals
from collections import OrderedDict
import warnings
import numpy as np


__all__ = ['Plugger']

cartStatusCodes = {0: 'empty', 1: 'noMaNGAplate', 2: 'MaNGA_complete',
                   3: 'MaNGA_noStarted', 4: 'MaNGA_started', 10: 'unknown'}
replaceMsgs = {0: 'empty cart', 1: 'replacing non-MaNGA plate',
               2: 'replacing complete MaNGA plate',
               3: 'replacing non-started MaNGA plate',
               4: 'replacing started MaNGA plate',
               10: 'replacing plate with unknown status'}


def getCartStatus(activePluggings, cartNumber):
    """Returns the status of the plate in a cart. The returned tuple is
    (cart_number, plate, status_number, status_code, completion)"""

    from Totoro.dbclasses.plate import Plate

    cartActivePluggings = [aP for aP in activePluggings
                           if aP.plugging.cartridge.number == cartNumber]

    if len(cartActivePluggings) == 0:
        return (cartNumber, None, 0, cartStatusCodes[0], 0)  # Empty cart
    elif len(cartActivePluggings) > 1:
        raise exceptions.TotoroError(
            'something went wrong. Cart #{0} has more than '
            'one active plugging'.format(cartNumber))
    else:
        cartActivePlugging = cartActivePluggings[0]

    plate = cartActivePlugging.plugging.plate

    isMaNGAPlate = (plate.currentSurveyMode is not None and
                    'MaNGA' in plate.currentSurveyMode.label)

    if not isMaNGAPlate:
        return (cartNumber, plate, 1, cartStatusCodes[1], 0)  # No MaNGA plate

    totoroPlate = Plate(plate)

    if totoroPlate.isComplete:
        return (cartNumber, totoroPlate, 2,
                cartStatusCodes[2], 1.)  # Complete MaNGA plate

    if totoroPlate.getPlateCompletion() == 0:
        return (cartNumber, totoroPlate, 3,
                cartStatusCodes[3], 0.)  # Non-stated MaNGA plate
    else:
        return (cartNumber, totoroPlate, 4, cartStatusCodes[4],
                totoroPlate.getPlateCompletion())  # Started MaNGA plate

    return (cartNumber, None, 10, cartStatusCodes[10], 0)  # Unknown


def getCartPlate(activePluggings, cartNumber):
    """Returns the plate plugged in a cart or None."""

    for aP in activePluggings:
        if aP.plugging.cartridge.number == cartNumber:
            return aP.plugging.plate
    return None


def getCartForReplug(plate):
    """Returns the cart of the last plugging."""

    if len(plate.pluggings) == 0:
        return None

    scanMJDs = [plugging.fscan_mjd for plugging in plate.pluggings]
    return plate.pluggings[np.argmax(scanMJDs)].cartridge.number


def prioritiseCarts(cartStatus, activePluggings):
    """Returns a list of carts sorted by priority for being allocated."""

    # Creates some intermediate lists
    empty = []
    noMaNGA = []
    complete = []
    noStarted = []
    unknown = []
    started = []

    # Assigns carts to the appropriate list.
    for cart in cartStatus:
        statusLabel = cart[3]
        if statusLabel == 'empty':
            empty.append(cart)
        elif statusLabel == 'noMaNGAplate':
            noMaNGA.append(cart)
        elif statusLabel == 'MaNGA_complete':
            complete.append(cart)
        elif statusLabel == 'MaNGA_noStarted':
            noStarted.append(cart)
        elif statusLabel == 'unknown':
            unknown.append(cart)
        elif statusLabel == 'MaNGA_started':
            started.append(cart)

    # Sorts started carts using the completion (fifth element of the tuple).
    started = sorted(started, key=lambda xx: xx[4])

    # Returns carts in the desired order.
    return empty + complete + unknown + noMaNGA + noStarted + started


class Plugger(object):
    """A class to schedule plugging requests."""

    def __init__(self, startDate=None, endDate=None, **kwargs):

        if startDate == 0.:
            startDate = None
        if endDate == 0.:
            endDate = None

        if startDate is None and endDate is None:
            self._initNoManga()
        elif any([startDate, endDate]) and not all([startDate, endDate]):
            raise exceptions.TotoroPluggerError(
                'either startDate=endDate=None or '
                'both dates need to be defined.')
        else:
            self._initFromDates(startDate, endDate, **kwargs)

    def _initNoManga(self):
        """Inits a Plugger instance when no MaNGA time is scheduled. The cart
        assignement contains only those plugged MaNGA plates that are not
        complete."""

        from Totoro.dbclasses import getPlugged

        self.startDate = None
        self.endDate = None

        warnings.warn(
            'no JD1, JD2 values provided. Plugger will only return plugged, '
            'non-completed plates.', exceptions.TotoroPluggerWarning)

        pluggedPlates = getPlugged(fullCheck=False, updateSets=False)

        self.carts = OrderedDict()
        self._nNewExposures = dict()

        for plate in pluggedPlates:
            if not plate.isComplete:
                cart = plate.getActiveCartNumber()
                self.carts[cart] = plate

        self.addCartOrder(metric='completion')

    def _initFromDates(self, jd0, jd1, **kwargs):
        """Initialites the Plugger instance from two JD dates."""

        assert jd0 < jd1

        self.startDate = jd0
        self.endDate = jd1
        log.info('Start date: {0}'.format(self.startDate))
        log.info('End date: {0}'.format(self.endDate))
        log.info('Scheduling {0:.2f} hours'.format(
                 (self.endDate - self.startDate) * 24.))

        self.timeline = Timeline(self.startDate, self.endDate, **kwargs)

        self._platesToSchedule = self.selectPlates(**kwargs)
        log.info('scheduling {0} plates'.format(len(self._platesToSchedule)))

        # Initialises a dictionary with the MaNGA carts. Removes offline carts.
        self.carts = OrderedDict([(key, None)
                                  for key in config['mangaCarts']
                                  if key not in config['offlineCarts']])

    def schedule(self, **kwargs):

        self._scheduleForced(**kwargs)

        # Removes force scheduled plates from the list of plates to schedule.
        forceScheduled = [plate.plate_id for plate in self.timeline.scheduled]
        for plate in self._platesToSchedule:
            if plate.plate_id in forceScheduled:
                self._platesToSchedule.remove(plate)

        if (len(self.timeline.scheduled) >= len(self.carts) or
                self.timeline.remainingTime <= 0):
            pass
        else:
            self.timeline.schedule(self._platesToSchedule, mode='plugger',
                                   **kwargs)

        # We log the number of new exposures for the plates in the timeline.
        # We'll use this later when we prioritise carts.
        self._nNewExposures = dict(
            [(plate.plate_id, len(plate.getMockExposures()))
             for plate in self.timeline.scheduled])

        self.allocateCarts(self.timeline.scheduled)
        self._cleanUp()  # Removes cart without MaNGA plates

        remainingTime = self.timeline.remainingTime
        if remainingTime > 0:
            log.important('{0:.2f}h hours not allocated'.format(remainingTime))
        else:
            log.debug('all time has been allocated.')

    def getASOutput(self, **kwargs):
        """Returns the plugging request as a cart dictionary, in a format
        that the master autoscheduler can understand."""

        if self.startDate is not None and self.endDate is not None:
            self.schedule(**kwargs)

        # First we add carts not used to cart_order, with lower priority
        nonUsedCarts = [cartNo for cartNo in config['mangaCarts']
                        if cartNo not in self.carts['cart_order']]

        self.carts['cart_order'] = nonUsedCarts + self.carts['cart_order']

        # We also add the APOGEE carts
        self.carts['cart_order'] = (config['apogeeCarts'][::-1] +
                                    self.carts['cart_order'])

        # We change the Totoro.Plate instances to plate_ids
        for key in self.carts:
            if key != 'cart_order' and self.carts[key] is not None:
                self.carts[key] = self.carts[key].plate_id

        return self.carts

    def selectPlates(self, onlyIncomplete=True, onlyMarked=False, **kwargs):
        """Selects plates to schedule, rejecting those which are invalid or
        must not be scheduled."""

        from Totoro import dbclasses

        onlyVisiblePlates = kwargs.pop('onlyVisiblePlates',
                                       config['plugger']['onlyVisiblePlates'])

        assert isinstance(onlyVisiblePlates, int), \
            'onlyVisiblePlates must be a boolean'

        log.info('getting plates at APO with onlyIncomplete={0}, '
                 'onlyMarked={1}'.format(onlyIncomplete, onlyMarked))

        if onlyVisiblePlates:
            lstRange = site.localSiderealTime([self.startDate, self.endDate])
            window = config['plateVisibilityMaxHalfWindowHours']
            raRange = np.array([(lstRange[0] - window) * 15.,
                                (lstRange[1] + window) * 15.])

            log.info('selecting plates with RA in range {0}'
                     .format(str(raRange % 360)))

            raRange = intervals.splitInterval(raRange, 360.)

        else:
            raRange = None

        plates = dbclasses.getAtAPO(onlyIncomplete=onlyIncomplete,
                                    onlyMarked=onlyMarked,
                                    rejectLowPriority=True,
                                    fullCheck=False, updateSets=False,
                                    raRange=raRange)

        log.info('plates found: {0}'.format(len(plates)))

        prioPlug = int(config['plugger']['forcePlugPriority'])

        # Selects plates with priority 10 or plugged
        platesToSchedule = []

        for plate in plates:
            if (plate.priority == prioPlug or plate.isPlugged):
                platesToSchedule.append(plate)

        # Adds the remainder of the plates
        for plate in [plate for plate in plates
                      if plate not in platesToSchedule]:

            plate_id = plate.plate_id

            if plate.priority <= config['plugger']['noPlugPriority']:
                log.debug('Skipped plate_id={0} because of low priority'
                          .format(plate_id))
                continue

            if plate.isComplete:
                log.debug('Skipped plate_id={0} because is complete'
                          .format(plate_id))
                continue

            # If the plate has been started but is not plugged and the cart
            # that must be used is already plugged, skips the plate.
            if plate.getPlateCompletion(includeIncompleteSets=True) > 0:
                plate.isReplug = True

                # Replacing this logic because now we allow a plate to be
                # replugged in a different cart.
                #
                # cartToUse = getCartForReplug(plate)
                # isCartFree = True
                # for pp in platesToSchedule:
                #     if (pp.isPlugged and
                #             pp.getActiveCartNumber() == cartToUse):
                #         isCartFree = False
                #         break
                # if not isCartFree:
                #     log.info('Skipped plate_id={0} because is a replug and '
                #              'its cart is in use.'.format(plate_id))
                #     continue
                # else:
                #     # Marks the plate for future reference
                #     plate.isReplug = True

            platesToSchedule.append(plate)

        return platesToSchedule

    def _scheduleForced(self, **kwargs):
        """Schedules plates that have priority=forcePlugPriority."""

        from Totoro.dbclasses import Plate

        db = getConnection()
        session = db.Session()

        forcePlugPriority = int(config['plugger']['forcePlugPriority'])

        # Does a query and gets all the plates with force plug priority
        with session.begin():
            forcePlugPlates = session.query(db.plateDB.Plate).join(
                db.plateDB.PlateToSurvey, db.plateDB.Survey,
                db.plateDB.SurveyMode, db.plateDB.PlatePointing
            ).filter(db.plateDB.Survey.label == 'MaNGA',
                     db.plateDB.SurveyMode.label.ilike('%MaNGA%'),
                     db.plateDB.PlatePointing.priority >= forcePlugPriority
                     ).order_by(db.plateDB.Plate.plate_id).all()

        if len(forcePlugPlates) == 0:
            return

        forcePlugPlates = [Plate(plate) for plate in forcePlugPlates]

        # Manually adds all the forced plates to the timeline.
        self.timeline.scheduled += [plate for plate in forcePlugPlates
                                    if plate not in self.timeline.scheduled]

    def _logCartAllocation(self, cartNumber, plate, messages=''):
        """Convenience function to log the cart allocation."""

        if plate is None:
            if messages == '':
                log.important('Cart #{0} -> empty'.format(cartNumber))
            else:
                log.important('Cart #{0} -> {1}'
                              .format(cartNumber, messages))
            return

        if not isinstance(messages, (list, tuple)):
            messageList = [messages]

        plateid = plate.plate_id
        status = plate.statuses[0].label

        if status == 'Shipped' and plate.location.label == 'APO':
            messageList.append('plate has not been marked')

        if hasattr(plate, 'isReplug') and plate.isReplug:
            messageList.append('replug')

        jointMessage = ', '.join(messageList)
        msgStr = '({0})'.format(jointMessage) if jointMessage != '' else ''

        log.important('Cart #{0} -> plate_id={1} {2}'
                      .format(cartNumber, plateid, msgStr))

    def allocateCarts(self, plates, **kwargs):
        """Allocates plates into carts in the most efficient way."""

        if len(plates) > len(self.carts):
            warnings.warn('{0} plates to allocate but only {1} carts '
                          'available. Using the first {1} plates.'
                          .format(len(plates), len(self.carts)),
                          exceptions.TotoroPluggerWarning)
            plates = plates[0:len(self.carts)]

        mjd = int(self.timeline.endDate - 2400000.5)
        log.important('Plugging allocation for MJD={0:d} follows:'
                      .format(mjd))

        # Dictionary to save the cart allocation
        cartPlateMessage = {}

        db = getConnection()
        session = db.Session()

        # Gets active pluggings
        with session.begin():
            activePluggings = session.query(
                db.plateDB.ActivePlugging).order_by(
                    db.plateDB.ActivePlugging.pk).all()

        for activePlugging in activePluggings:
            if activePlugging.pk in config['offlineCarts']:
                self.carts[activePlugging.pk] = None

        # Gets the status of the plates in each remaining cart.
        cartStatus = OrderedDict(
            [(cartNumber, getCartStatus(activePluggings, cartNumber))
             for cartNumber in self.carts if self.carts[cartNumber] is None])

        allocatedPlates = []

        # Allocates plates that are already plugged
        for plate in plates:
            if plate.isPlugged:
                cartNumber = plate.getActiveCartNumber()
                self.carts[cartNumber] = plate
                cartPlateMessage[cartNumber] = (plate, 'already plugged')
                allocatedPlates.append(plate)
                cartStatus.pop(cartNumber)

        # Allocates replugs
        for plate in plates:
            if plate in allocatedPlates:
                continue
            if hasattr(plate, 'isReplug') and plate.isReplug:
                cartNumber = getCartForReplug(plate)
                statusCode = cartStatus[cartNumber][2]
                if (cartNumber is not None and
                        cartNumber not in config['offlineCarts']):
                    self.carts[cartNumber] = plate
                    allocatedPlates.append(plate)
                    cartPlateMessage[cartNumber] = (plate,
                                                    replaceMsgs[statusCode])
                    cartStatus.pop(cartNumber)
                else:
                    log.debug('not plugging plate {0} in its original cart {1}'
                              ' because it is not available'
                              .format(plate.plate_id, cartNumber))
                    continue

        # Sorts carts by priority. Note that sortedCarts is a list of tuples,
        # while cartStatus was a dictionary.
        sortedCarts = prioritiseCarts(cartStatus.values(), activePluggings)

        # Allocates the remaining plates
        for plate in plates:
            if plate in allocatedPlates:
                continue

            cart = sortedCarts[0]
            sortedCarts.pop(0)

            cartNumber, cartPlate, statusCode, statusLabel, completion = cart

            self.carts[cartNumber] = plate
            allocatedPlates.append(plate)
            msg = replaceMsgs[statusCode]
            if statusLabel == 'MaNGA_started':
                msg += ', completion={0:.2f}'.format(completion)

            cartPlateMessage[cartNumber] = (plate, msg)

        if len(plates) > len(allocatedPlates):
            warnings.warn('{0} plates have not been allocated'.format(
                          len(plates) - len(allocatedPlates)),
                          exceptions.TotoroPluggerWarning)

        # Checks unassigned carts
        for cart in sortedCarts:
            cartNumber, plate, statusCode, statusLabel, completion = cart
            if completion >= 1:
                # Unplugs complete plates
                cartPlateMessage[cartNumber] = (plate, 'unplug')
                continue
            else:
                if statusLabel != 'noMaNGAplate' and plate is not None:
                    # If this is a MaNGA plate, keeps it.
                    self.carts[cartNumber] = plate
                    if plate.isPlugged:
                        cartPlateMessage[cartNumber] = (plate,
                                                        'already plugged')
                    else:
                        cartPlateMessage[cartNumber] = (plate, 'unchanged')
                else:
                    # Otherwise, does nothing.
                    cartPlateMessage[cartNumber] = (plate,
                                                    'not doing anything')

        # Logs the allocation
        for cartNumber in sorted(cartPlateMessage.keys()):
            self._logCartAllocation(cartNumber,
                                    cartPlateMessage[cartNumber][0],
                                    cartPlateMessage[cartNumber][1])

        # Now we add a list with the priority order of the allocated carts.
        # This is useful if APOGEE needs to take over some of our carts.
        # In this way, they'll first use our carts with lower priority.
        self.addCartOrder(metric='scheduled')

    def addCartOrder(self, metric='scheduled'):
        """Adds a key `cart_order` to self.carts with the priority of the carts

        If ``metric='scheduled'``, priority (from low to high) goes as it
        follows:

        (1) Completed plates. Note that there should be no completed plates
            in self.carts, but this is just to double-check.
        (2) Other plates, sorted by the number of exposures needed to fill out
            the night.
        (3) Force plug plates (plates with priority 10)

        If ``metric='completion'``, (2) is replaced with the number of
        incomplete sets. Plates with incomplete sets are given the highest
        priority. This is used when `addCartOrder` is called for a `Plugger`
        object initialised without dates, and not scheduling is performed.

        Additionally, we give high priority to offline cart. This is to avoid
        APOGEE to plug co-designed plates in those carts if possible. If
        ``metric='scheduled'``, offline carts are given the highest priority
        after all scheduled carts. If ``metric='completion'`` offline carts are
        given higher priority than any but carts with incomplete sets.

        """

        assert metric in ['scheduled', 'completion'], \
            'metric must be \'scheduled\' or \'completion\''

        forcePlugPriority = int(config['plugger']['forcePlugPriority'])

        # Splits plates in the three priority categories.
        completed = []
        scheduled = []
        forcePlug = []

        for cart, plate in self.carts.iteritems():
            if plate is None:
                continue
            if plate.priority == forcePlugPriority:
                forcePlug.append((cart, plate))
            elif plate.getPlateCompletion(useMock=False) > 1:
                completed.append((cart, plate))
            else:
                scheduled.append((cart, plate))

        if metric == 'scheduled':

            # Retrieves how many scheduled (mock) exposures are in each plate.
            nExposures = [self._nNewExposures[plate.plate_id]
                          if plate.plate_id in self._nNewExposures else 0
                          for cart, plate in scheduled]

            # Sorts scheduled exposures from few to many scheduled exposures.
            scheduledOrdered = [scheduled[ii] for ii in np.argsort(nExposures)]

            # Sorts scheduled exposures from few to many scheduled exposures.
            scheduledOrdered = [scheduled[ii] for ii in np.argsort(nExposures)]

        elif metric == 'completion':

            # Finds out what plates have incomplete sets.
            platesWithIncompleteSets = []
            platesWithoutIncompleteSets = []

            for ii in range(len(scheduled)):
                if scheduled[ii][1].hasIncompleteSets():
                    platesWithIncompleteSets.append(scheduled[ii])
                else:
                    platesWithoutIncompleteSets.append(scheduled[ii])

            # Calculates completion for each plate. For plates with incomplete
            # sets we take them into account.
            completionWithIncompleteSets = [
                plate.getPlateCompletion(includeIncompleteSets=True)
                for cart, plate in platesWithIncompleteSets]

            completionWithoutIncompleteSets = [
                plate.getPlateCompletion()
                for cart, plate in platesWithoutIncompleteSets]

            # Sorts the scheduled plates according to completion, with plates
            # without incomplete sets always first.
            scheduledOrdered = [
                platesWithoutIncompleteSets[ii]
                for ii in np.argsort(completionWithoutIncompleteSets)] + \
                [platesWithIncompleteSets[jj]
                 for jj in np.argsort(completionWithIncompleteSets)]

        usedCarts = [cart for cart, plate in
                     completed + scheduledOrdered + forcePlug]

        # Creates master ordered list
        if metric == 'scheduled':
            offline = [(cart, None) for cart in config['offlineCarts']
                       if cart not in usedCarts]
            orderedCarts = completed + offline + scheduledOrdered + forcePlug
        else:
            # Identifies the first plate with incomplete sets
            ii = 0
            for cart, plate in scheduledOrdered:
                if np.any([ss.getStatus()[0] == 'Incomplete'
                           for ss in plate.sets]):
                    break
                ii += 1

            # Adds offline carts before
            for cart in config['offlineCarts']:
                if cart in usedCarts:
                    continue
                scheduledOrdered.insert(ii, (cart, None))

            orderedCarts = completed + scheduledOrdered + forcePlug

        # Now it adds the list to self.carts
        self.carts['cart_order'] = [cart for cart, plate in orderedCarts]

        return

    def _cleanUp(self):
        """Removes keys in self.cart whose value is None."""

        keysToRemove = []
        for key in self.carts:
            if self.carts[key] is None:
                keysToRemove.append(key)
            else:
                pass

        for key in keysToRemove:
            self.carts.pop(key)
