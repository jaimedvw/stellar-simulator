"""
=========================
Value
=========================

Author: Matija Piskorec
Last update: August 2023

Value class.
"""

from Log import log
from Transaction import Transaction
from State import State

import random

class Value():

    def __init__(self,**kwargs):

        self._transactions = kwargs['transactions'] if 'transactions' in kwargs else []

        assert all([isinstance(transaction,Transaction) for transaction in self._transactions])

        self._hash = hash(frozenset(self._transactions))
        self._state = kwargs['state'] if 'state' in kwargs else State.init

        log.value.info('Created value, hash = %s, state = %s, transactions = %s',
                       self._hash,
                       self._state,
                       self._transactions)

    def __repr__(self):
        return '[Value, hash = %s, state = %s, transactions = %s]' % (self._hash,self._state,self._transactions)

    def __eq__(self, other):
        return (self.hash == other.hash and self.state == other.state and set(self.transactions) == set(other.transactions))


    def __hash__(self):
        return hash(self._hash)

    @property
    def transactions(self):
        return self._transactions

    @property
    def state(self):
        return self._state

    @property
    def hash(self):
        return self._hash

    @classmethod
    def combine(cls, values):
        """
        Combine a list of Value objects into a single Value by taking the union
        of their transactions. Duplicates are eliminated because they are collected in a set.
        """
        combined_txs = set()
        for value in values:
            combined_txs.update(value.transactions)
        return Value(transactions=combined_txs)

    @transactions.setter
    def transactions(self, tx_list):
        # sanity check
        assert all(isinstance(tx, Transaction) for tx in tx_list)
        self._transactions = tx_list
        # recompute your internal hash so equality still works
        self._hash = hash(frozenset(self._transactions))