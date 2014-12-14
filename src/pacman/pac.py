#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  pac.py
#
#  This code is based on previous work by Rémy Oudompheng <remy@archlinux.org>
#
#  Copyright © 2013,2014 Antergos
#
#  This file is part of Cnchi.
#
#  Cnchi is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  Cnchi is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Cnchi; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

""" Module interface to pyalpm """

import traceback
import sys
import math
import logging
import os

if __name__ == "__main__":
    import gettext
    _ = gettext.gettext

try:
    import pyalpm
except ImportError as err:
    pass

try:
    import pacman.config as config
except ImportError as err:
    # When testing pac.py
    import config

import queue

class Pac(object):
    """ Comunicates with libalpm using pyalpm """
    def __init__(self, conf_path="/etc/pacman.conf", callback_queue=None):
        self.callback_queue = callback_queue

        self.conflict_to_remove = None

        self.handle = None

        # Some download indicators (used in cb_dl callback)
        self.last_dl_filename = None
        self.last_dl_progress = 0
        self.last_dl_total_size = 0

        # Total packages to download
        self.total_packages_to_download = 0
        self.downloaded_packages = 0

        # Store package total download size
        self.total_download_size = 0

        self.last_event = {}

        if conf_path != None and os.path.exists(conf_path):
            self.config = config.PacmanConfig(conf_path)
            self.initialize_alpm()
        else:
            raise pyalpm.error

    def get_handle(self):
        return self.handle

    def get_config(self):
        return self.config

    def initialize_alpm(self):
        root_dir = "/"
        db_path = "/var/lib/pacman"
        if self.config is not None:
            root_dir = self.config.options["RootDir"]
            db_path = self.config.options["DBPath"]

        self.handle = pyalpm.Handle(root_dir, db_path)

        if self.handle is not None:
            if self.config is not None:
                self.config.apply(self.handle)
            # Set callback functions
            self.handle.logcb = self.cb_log
            self.handle.dlcb = self.cb_dl
            self.handle.totaldlcb = self.cb_totaldl
            self.handle.eventcb = self.cb_event
            self.handle.questioncb = self.cb_question
            self.handle.progresscb = self.cb_progress

    def release(self):
        if self.handle is not None:
            del self.handle
            self.handle = None

    def finalize_transaction(self, transaction):
        """ Commit a transaction """
        all_ok = True
        try:
            transaction.prepare()
            transaction.commit()
        except pyalpm.error as err:
            msg = _("Can't finalize alpm transaction: %s") % err
            logging.error(msg)
            all_ok = False
        finally:
            transaction.release()
            return all_ok

    def init_transaction(self, options={}):
        """ Transaction initialization """
        try:
            transaction = self.handle.init_transaction(
                cascade=options.get('cascade', False),
                nodeps=options.get('nodeps', False),
                force=options.get('force', False),
                dbonly=options.get('dbonly', False),
                downloadonly=options.get('downloadonly', False),
                needed=options.get('needed', False),
                nosave=options.get('nosave', False),
                recurse=(options.get('recursive', 0) > 0),
                recurseall=(options.get('recursive', 0) > 1),
                unneeded=options.get('unneeded', False),
                alldeps=(options.get('mode', None) == pyalpm.PKG_REASON_DEPEND),
                allexplicit=(options.get('mode', None) == pyalpm.PKG_REASON_EXPLICIT))
        except pyalpm.error as err:
            msg = _("Can't init alpm transaction: %s") % err
            logging.error(msg)
            transaction = None
        finally:
            return transaction

    def do_refresh(self):
        """ Sync databases like pacman -Sy """
        if self.handle is None:
            logging.error("alpm is not initialised")
            raise pyalpm.error

        force = True
        for db in self.handle.get_syncdbs():
            transaction = self.init_transaction()
            db.update(force)
            transaction.release()
        return True

    def do_install(self, pkgs, conflicts=[], options={}):
        """ Install a list of packages like pacman -S """
        if self.handle is None:
            logging.error("alpm is not initialised")
            raise pyalpm.error

        logging.debug(_("Cnchi will install a list of packages like pacman -S"))

        if len(pkgs) == 0:
            return []

        # Discard duplicates
        pkgs = list(set(pkgs))

        repos = dict((db.name, db) for db in self.handle.get_syncdbs())

        targets = []
        for name in pkgs:
            ok, pkg = self.find_sync_package(name, repos)
            if ok:
                # Check that added package is not in our conflicts list
                if pkg.name not in conflicts:
                    targets.append(pkg.name)
            else:
                # Couldn't find the package, check if it's a group
                group_pkgs = self.get_group_pkgs(name)
                if group_pkgs != None:
                    # It's a group
                    for group_pkg in group_pkgs:
                        # Check that added package is not in our conflicts list
                        # Ex: connman conflicts with netctl(openresolv),
                        # which is installed by default with base group
                        if group_pkg.name not in conflicts:
                            targets.append(group_pkg.name)
                else:
                    # No, it wasn't neither a package nor a group. As we don't know if
                    # this error is fatal or not, we'll register it and we'll allow to continue.
                    logging.error(_("Can't find a package or group called '%s'"), name)

        # Discard duplicates
        targets = list(set(targets))

        if len(targets) == 0:
            logging.error(_("No targets found"))
            return False

        trans = self.init_transaction(options)

        if trans is None:
            return False

        for name in targets:
            ok, pkg = self.find_sync_package(name, repos)
            logging.debug(_("Adding package '%s' to transaction"), pkg.name)
            trans.add_pkg(pkg)

        self.total_packages_to_download = len(targets)

        logging.debug(_("Run and finalize transaction..."))
        return self.finalize_transaction(trans)

    def find_sync_package(self, pkgname, syncdbs):
        """ Finds a package name in a list of DBs """
        for db in syncdbs.values():
            pkg = db.get_pkg(pkgname)
            if pkg is not None:
                return True, pkg
        return False, "Package '%s' was not found." % pkgname

    def get_group_pkgs(self, group):
        """ Get group packages """
        for repo in self.handle.get_syncdbs():
            grp = repo.read_grp(group)
            if grp is None:
                continue
            else:
                name, pkgs = grp
                return pkgs
        return None

    def queue_event(self, event_type, event_text=""):
        """ Queues events to the event list in the GUI thread """
        if event_type in self.last_event:
            if self.last_event[event_type] == event_text:
                # Do not enqueue the same event twice
                return

        self.last_event[event_type] = event_text

        if event_type == "error":
            # Format message to show file, function, and line where the error was issued
            import inspect
            # Get the previous frame in the stack, otherwise it would be this function
            func = inspect.currentframe().f_back.f_code
            # Dump the message + the name of this function to the log.
            event_text = "%s: %s in %s:%i" % (event_text, func.co_name, func.co_filename, func.co_firstlineno)

        if self.callback_queue is None:
            #print(event_type, event_text)
            if event_type == "error":
                sys.exit(1)
            else:
                return

        try:
            self.callback_queue.put_nowait((event_type, event_text))
        except queue.Full as err:
            pass

        if event_type == "error":
            # We've queued a fatal event so we must exit installer_process process
            # wait until queue is empty (is emptied in slides.py, in the GUI thread), then exit
            self.callback_queue.join()
            sys.exit(1)

    # Callback functions

    def cb_question(self, *args):
        """ Called to get user input """
        pass

    def cb_totaldl(self, total_size):
        """ Stores total download size for use in cb_progress """
        self.total_download_size = total_size

    def cb_event(self, ID, event, tupel):
        """ Converts action ID to descriptive text and enqueues it to the events queue """
        action = ""

        if ID is 1:
            action = _('Checking dependencies...')
        elif ID is 3:
            action = _('Checking file conflicts...')
        elif ID is 5:
            action = _('Resolving dependencies...')
        elif ID is 7:
            action = _('Checking inter conflicts...')
        elif ID is 9:
            # action = _('Installing...')
            action = ""
        elif ID is 11:
            action = _('Removing...')
        elif ID is 13:
            action = _('Upgrading...')
        elif ID is 15:
            action = _('Checking integrity...')
        elif ID is 17:
            action = _('Loading packages files...')
        elif ID is 26:
            action = _('Configuring...')
        elif ID is 27:
            action = _('Downloading a file')
        else:
            action = ""

        if len(action) > 0:
            self.queue_event('info', action)

    def cb_log(self, level, line):
        """ Log pyalpm warning and error messages.
            Possible message types:
            LOG_ERROR, LOG_WARNING, LOG_DEBUG, LOG_FUNCTION """

        # Only manage error and warning messages
        if level & pyalpm.LOG_ERROR:
            logging.error(line)
        elif level & pyalpm.LOG_WARNING:
            logging.warning(line)

    def cb_progress(self, target, percent, n, i):
        """ Shows install progress """
        if target:
            msg = _("Installing %s (%d/%d)") % (target, i, n)
            percent = i / n
        else:
            msg = _("Checking and loading packages... (%d targets)") % n
            percent = percent / 100

        self.queue_event('info', msg)
        self.queue_event('percent', percent)


    def cb_dl(self, filename, tx, total):
        """ Shows downloading progress """
        # Check if a new file is coming
        if filename != self.last_dl_filename or self.last_dl_total_size != total:
            self.last_dl_filename = filename
            self.last_dl_total_size = total
            self.last_dl_progress = 0

            # If pacman is just updating databases total_download_size will be zero
            if self.total_download_size == 0:
                ext = ".db"
                if filename.endswith(ext):
                    filename = filename[:-len(ext)]
                text = _("Updating %s database") % filename
            else:
                ext = ".pkg.tar.xz"
                if filename.endswith(ext):
                    filename = filename[:-len(ext)]
                self.downloaded_packages = self.downloaded_packages + 1
                i = self.downloaded_packages
                n = self.total_packages_to_download
                #text = _("Downloading %s... (%d/%d)") % (filename, i, n)
                text = _("Downloading %s...") % filename

            self.queue_event('info', text)
            self.queue_event('percent', 0)
        else:
            # Compute a progress indicator
            if self.last_dl_total_size > 0:
                progress = tx / self.last_dl_total_size
            else:
                # If total is unknown, use log(kBytes)²/2
                progress = (math.log(1 + tx / 1024) ** 2 / 2) / 100

            # Update progress only if it has grown
            if progress > self.last_dl_progress:
                #logging.debug("filename [%s], tx [%d], total [%d]", filename, tx, total)
                self.last_dl_progress = progress
                self.queue_event('percent', progress)

''' Test case '''
if __name__ == "__main__":
    import gettext
    _ = gettext.gettext

    formatter = logging.Formatter(
        '[%(asctime)s] [%(module)s] %(levelname)s: %(message)s',
        "%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        pacman = Pac("/etc/pacman.conf")
    except Exception as err:
        print("Can't initialize pyalpm: %s" % err)
        sys.exit(1)

    try:
        pacman.do_refresh()
    except pyalpm.error as err:
        print("Can't update databases: %s" % err)
        sys.exit(1)

    pacman_options = {}
    pacman_options["downloadonly"] = True
    #pacman.do_install(pkgs=["base"], conflicts=[], options=pacman_options)
    pacman.release()
