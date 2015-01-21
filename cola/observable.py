# Copyright (C) 2007-2018 David Aguilar and contributors
"""This module provides the Observable class"""
from __future__ import division, absolute_import, unicode_literals

from collections import defaultdict


class Observable(object):
    """Handles subject/observer notifications."""

    def __init__(self):
        self.notification_enabled = True
        self.observers = defaultdict(set)

    def add_observer(self, message, observer):
        """Add an observer for a specific message."""
        self.observers[message].add(observer)

    def remove_observer(self, observer):
        """Remove an observer."""
        for observers in self.observers.values():
            observers.discard(observer)

    def notify_observers(self, message, *args, **opts):
        """Pythonic signals and slots."""
        if not self.notification_enabled:
            return
        # observers can remove themselves during their callback so grab a copy
        observers = set(self.observers.get(message, set()))
        for method in observers:
            method(*args, **opts)
