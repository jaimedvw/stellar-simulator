"""
=========================
Node
=========================

Author: Matija Piskorec, Jaime de Vivero Woods

Last update: March 2025

Node class.

Documentation:

[2] Nicolas Barry and Giuliano Losa and David Mazieres and Jed McCaleb and Stanislas Polu, The Stellar Consensus Protocol (SCP) - technical implementation draft, https://datatracker.ietf.org/doc/draft-mazieres-dinrg-scp/05/
"""
import math
import random
from collections import deque

import numpy as np
from Log import log
from Event import Event
from Ledger import Ledger
from QuorumSet import QuorumSet
from SCPNominate import SCPNominate
from SCPBallot import SCPBallot
from SCPPrepare import SCPPrepare
from SCPCommit import SCPCommit
from SCPExternalize import SCPExternalize
from Value import Value
from Storage import Storage
from Globals import Globals
import copy
import xdrlib3
import hashlib
import os
from typing import List


class Node():
    name = None
    quorum_set = None
    ledger = None
    mempool = None
    storage = None
    nomination_round = None

    def __init__(self, name, quorum_set=None, ledger=None, storage=None):
        self.name = name
        self.quorum_set = quorum_set if quorum_set is not None else QuorumSet(self)
        self.ledger = ledger if ledger is not None else Ledger(self)
        self.slot = 1
        self.mempool = None
        self.tx_queue = deque()

        self.storage = storage if storage is not None else Storage(self)
        default_state = {'voted': [], 'accepted': [], 'confirmed': []}
        self.nomination_state = copy.deepcopy(default_state)
        self.balloting_state = copy.deepcopy(default_state)
        self.statement_counter = {} # This hashmap (or dictionary) keeps track of all Values added and how many times unique nodes have made statements on it
        # This dictionary looks like this {Value_hash: {'voted': {node_id: count,...}}, {'accepted': {node_id:count}}}
        self.broadcast_flags = []  # Add every message here for other
        self.received_broadcast_msgs = {} # This hashmap (or dictionary) keeps track of all Messages retrieved by each node
        # This dictionary looks like this {{node.name: SCPNominate,...},...}
        self.priority_list = set()
        self.finalised_transactions = set()
        self._seen_finalised_ballots = set()
        self.MAX_SLOT_TXS = 200

        self.nomination_round = 1
        self.last_nomination_start_time = 0.0

        ###################################
        # PREPARE BALLOT PHASE STRUCTURES #
        ###################################
        self.balloting_state = {'voted': {}, 'accepted': {}, 'confirmed': {}, 'aborted': {}} # This will look like: self.balloting_state = {'voted': {'value_hash_1': SCPBallot(counter=1, value=ValueObject1),},'accepted': { 'value_hash_2': SCPBallot(counter=3, value=ValueObject2)},'confirmed': { ... },'aborted': { ... }}
        self.ballot_statement_counter = {} # This will use sets for node names as opposed to counts, so will look like: {SCPBallot1.value: {'voted': set(Node1), ‘accepted’: set(Node2, Node3), ‘confirmed’: set(), ‘aborted’: set(), SCPBallot2.value: {'voted': set(), ‘accepted’: set(), ‘confirmed’: set(), ‘aborted’: set(node1, node2, node3)}
        self.ballot_prepare_broadcast_flags = set() # Add every SCPPrepare message here - this will look like
        self.received_prepare_broadcast_msgs = {}
        self.prepared_ballots = {} # This looks like: self.prepared_ballots[ballot.value] = SCPPrepare('aCounter': aCounter,'cCounter': cCounter,'hCounter': hCounter,'highestCounter': ballot.counter)

        ###################################
        # SCPCOMMIT BALLOT PHASE STRUCTURES #
        ###################################
        self.commit_ballot_state = {'voted': {}, 'accepted': {}, 'confirmed': {}} # This will look like: self.balloting_state = {'voted': {'value_hash_1': SCPBallot(counter=1, value=ValueObject1),},'accepted': { 'value_hash_2': SCPBallot(counter=3, value=ValueObject2)},'confirmed': { ... },'aborted': { ... }}
        self.commit_ballot_statement_counter = {} # This will use sets for node names as opposed to counts, so will look like: {SCPBallot1.value: {'voted': set(Node1), ‘accepted’: set(Node2, Node3), ‘confirmed’: set(), ‘aborted’: set(), SCPBallot2.value: {'voted': set(), ‘accepted’: set(), ‘confirmed’: set(), ‘aborted’: set(node1, node2, node3)}
        self.commit_ballot_broadcast_flags = set() # Add every SCPPrepare message here - this will look like
        self.received_commit_ballot_broadcast_msgs = {}
        self.committed_ballots = {} # This looks like: self.prepared_ballots[ballot.value] = SCPPrepare('aCounter': aCounter,'cCounter': cCounter,'hCounter': hCounter,'highestCounter': ballot.counter)

        ###################################
        # EXTERNALIZE PHASE STRUCTURES    #
        ###################################
        self.externalize_broadcast_flags = set() # Change to store (slot, message) tuples
        self.externalized_slot_counter = set()
        self.peer_externalised_statements = {} # This will be used to track finalised slots for nodes, so will look like: {Node1: set(SCPExternalize(ballot, 1), SCPExternalize(ballot2, 3), Node2:{})}

        log.node.info('Initialized node %s, quorum_set=%s, ledger=%s, storage=%s.',
                      self.name,
                      self.quorum_set,
                      self.ledger,
                      self.storage)


        self.log_path = 'simulator_events_log.txt'
        self.log_nomination_data_path = 'nomination_phase_log.txt'

    def remove_finalized_transactions(self, finalized_value):
        """
        Remove all Values from all data structures where any transaction in the finalized value appears.
        """
        finalized_transactions = set(finalized_value.transactions)  # Extract finalized transactions

        def filter_values(state_dict):
            """
            Helper function to remove values containing finalized transactions.
            """
            for key in state_dict.keys():
                state_dict[key] = [
                    value for value in state_dict[key]
                    if hasattr(value, "transactions") and not finalized_transactions.intersection(value.transactions)
                ]

        filter_values(self.nomination_state)

        # Remove entries from statement_counter that contain finalized transactions
        self.statement_counter = {
            value: count for value, count in self.statement_counter.items()
            if hasattr(value, "transactions") and not finalized_transactions.intersection(value.transactions)
        }

        log.node.info("Node %s removed all values and ballots containing finalized transactions.", self.name)

    #### LOGGER FUNCTION
    def log_to_file(self, message):
        with open(self.log_path, 'a') as log_file:
            log_file.write(f"{Globals.simulation_time:.2f} - {message}\n")

    def __repr__(self):
        return '[Node: %s]' % self.name

    def __eq__(self, name):
        return self.name == name

    def __hash__(self):
        return hash(self.name)

    @classmethod
    def get_events(cls):
        events = [Event('node')]
        log.consensus.info('Sending Node events %s.' %events)
        return events

    def extract_transaction_id(self, transaction):
        return transaction.hash

    def is_transaction_in_externalized_slots(self, transaction_id):
        for externalized_message in self.externalized_slot_counter:
            ballot = externalized_message.ballot
            if ballot and hasattr(ballot, 'value') and hasattr(ballot.value, 'transactions'):
                if any(tx.hash == transaction_id for tx in ballot.value.transactions):
                    return True
        return False

    def is_message_externalized(self, message):
        """
        Checks if the transactions in a broadcast message have been externalized (approved).
        This is just an example, you should define it according to your externalization criteria.
        """
        for value in message.voted:
            if hasattr(value, 'transactions'):
                for tx in value.transactions:
                    if self.is_transaction_in_externalized_slots(tx.hash):
                        return True
        # Check the 'accepted' values
        for value in message.accepted:
            if hasattr(value, 'transactions'):
                for tx in value.transactions:
                    if self.is_transaction_in_externalized_slots(tx.hash):
                        return True
        return False


    def calculate_nomination_round(self):
        """
        A node always begins nomination in round "1". Round "n" lasts for
        "1+n" seconds, after which, if no value has been confirmed nominated,
        the node proceeds to round "n+1". A node continues to echo votes
        from the highest priority neighbor in prior rounds as well as the
        current round. In particular, until any value is confirmed
        nominated, a node continues expanding its "voted" field with values
        nominated by highest priority neighbors from prior rounds even when
        the values appeared after the end of those prior rounds
        """
        if self.slot <= 0 or not self.ledger.get_slot(self.slot - 1): # check that there is a slot which externalised
            log.node.error("No externalized message available for previous slot.")
            return None

        previous_slot_message = self.ledger.get_slot(self.slot - 1)
        previous_timestamp = previous_slot_message.timestamp

        current_time = Globals.simulation_time
        time_diff = current_time - previous_timestamp
        self.last_nomination_start_time = current_time

        round = 1
        cumulative_time = 1 + round  # the finish time of round 1 is 1 + 1 = 2

        while time_diff > cumulative_time:
            round += 1
            cumulative_time += (1 + round)  # add the duration of the next round

        log.node.debug('Node %s is in round %d based on the timestamp of slot %d (time difference: %.2f seconds)',
                       self.name, round, self.slot - 1, time_diff)

        return round

    def check_update_nomination_round(self):
        """
        1. increase nomination round count and update priority list
        2. Each round lasts (1+round) - so check if last nomination start time + nomination_round < global.simulation_time, and update if True
        """
        if Globals.simulation_time > (self.last_nomination_start_time + self.nomination_round):
            self.nomination_round += 1
            self.get_priority_list()
            log.node.info("Node %s updated its Nomination Round to %s", self.name, self.nomination_round)

    def retrieve_transaction_from_mempool(self):
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'w') as log_file:
                log_file.write("")

        transaction = self.mempool.get_transaction()
        if transaction:
            transaction_id = self.extract_transaction_id(transaction)

            if not self.is_transaction_in_externalized_slots(transaction_id):
                log.node.info('Node %s retrieved %s from mempool.', self.name, transaction)
                self.log_to_file(f"NODE - INFO - Node {self.name} retrieved {transaction} from mempool.")
                self.ledger.add(transaction)
                return transaction  # Return the valid transaction
            else:
                log.node.info('Node %s ignored transaction %s as it was already externalized.', self.name,
                              transaction_id)
                # Do not add the transaction to the ledger in this branch
                self.mempool.transactions.remove(transaction)
                self.log_to_file(
                    f"NODE - INFO - Node {self.name} ignored {transaction} as it was already externalized.")
                return None  # Explicitly return None as the transaction is externalized
        else:
            log.node.info('Node %s cannot retrieve transaction from mempool because it is empty!', self.name)
            return None

    def add_to_quorum(self, nodes):
        self.quorum_set.add(nodes)
        return

    # Set quorum to the nodes
    def set_quorum(self, nodes, inner_sets, threshold=None):
        if threshold is not None:
            self.quorum_set.threshold = threshold
        self.quorum_set.set(nodes=nodes, inner_sets=inner_sets)
        return

    def attach_mempool(self, mempool):
        self.mempool = mempool
        return

    def collect_finalised_transactions(self):
        """
        Scan through all externalize messages this node has ever sent,
        and update Node.finalised_transactions with each unique ballot’s tx hashes.
        """
        if self.slot == 1: # no slots finalised yet
            return
        for ext_msg in self.externalized_slot_counter:
            ballots = ext_msg.ballot
            if not isinstance(ballots, (list, tuple, set)):
                ballots = [ballots]

            for b in ballots:
                key = (b.counter, b.value) # identify a ballot uniquely by its counter and value
                if key not in self._seen_finalised_ballots:
                    self._seen_finalised_ballots.add(key)
                    # assume b.value.transactions always exists
                    self.finalised_transactions.update(
                        tx.hash for tx in b.value.transactions
                    )
        log.node.info("Added %s to finalised_transaction set", self.finalised_transactions)
        return self.finalised_transactions

    def nominate(self):
        """
        Broadcast SCPNominate message to the storage.
        """
        self.check_update_nomination_round()
        self.get_priority_list()
        if self in self.priority_list:
            msg = self.prepare_nomination_msg() # Prepares Values for Nomination and broadcasts message
            if msg is None:
                return
            voted_val = msg.voted[0]
            accepted_val = msg.accepted[0] if msg.accepted else None

            if type(voted_val) is Value and self.check_Quorum_threshold(voted_val):
                log.node.info('Quorum threshold met for voted value %s at Node %s after preparation', voted_val, self.name)
                self.update_nomination_state(voted_val, "voted")

            if type(accepted_val) is Value and self.check_Quorum_threshold(accepted_val):
                log.node.info('Quorum threshold met for accepted value %s at Node %s after preparation', accepted_val, self.name)
                self.update_nomination_state(accepted_val, "accepted")
        else:
            log.node.info("Node %s did not Nominate a Value since it is not in it's priority list", self.name)

        return

    def retrieve_broadcast_message(self, providing_node):
        """
        Pull one unseen envelope from providing_node.broadcast_flags.
        Return None once I've seen them all, or they have none left.
        """
        # nothing to pull if they have no flags
        if not providing_node.broadcast_flags:
            return None

        seen = self.received_broadcast_msgs.setdefault(providing_node.name, [])

        # collect the ones they still have that I haven't seen
        unseen = [m for m in providing_node.broadcast_flags if m not in seen]
        if not unseen:
            return None

        msg = np.random.choice(unseen)
        seen.append(msg)

        # if that nomination has already been externalized, drop it at the source
        if self.is_message_externalized(msg):
            providing_node.broadcast_flags.remove(msg)

        return msg

    def get_finalized_transaction_ids(self):
        """
        Collects all finalized transaction IDs (hashes) from the ledger.
        """
        finalized_tx_ids = set()
        for slot, ext_msg in self.ledger.slots.items():

            if isinstance(ext_msg, dict) and "ballot" in ext_msg:
                ballot = ext_msg["ballot"]
            else:
                ballot = getattr(ext_msg, "ballot", None)
            if ballot is None:
                continue

            if hasattr(ballot, "value"):
                value = ballot.value
            elif isinstance(ballot, dict) and "value" in ballot:
                value = ballot["value"]
            else:
                value = None
            if value is None:
                continue

            if hasattr(value, "transactions"):
                for tx in value.transactions:
                    finalized_tx_ids.add(tx.hash)
        return finalized_tx_ids

    def receive_message(self):

        # This checks if the node has no quorum set, if so then it simply gets ignored
        if not self.quorum_set or (not self.quorum_set.get_nodes() and not self.quorum_set.get_inner_sets()):
            log.node.warning(f"Node {self.name} has no valid quorum set! Skipping priority calculation.")
            return

        self.check_update_nomination_round()
        priority_node = self.get_highest_priority_neighbor()
        if len(self.priority_list) < 1:
            log.node.info("Node %s has no valid priority neighbor!", self.name)
            return

        for priority_node in self.priority_list:
            if priority_node != self:

                message = self.retrieve_broadcast_message(priority_node)

                if message is not None:
                    log.node.critical('Node %s receiving SCPNominate message', self.name, priority_node.name)
                    message = message.parse_message_state(message) # message is an array of 2 arrays, the first being the voted values and the second the accepted values
                    self.process_received_message(message)
                    self.update_statement_count(priority_node, message)
                    log.node.info('Node %s retrieving messages from his highest priority neighbor Node %s!', self.name,priority_node.name)

                    voted_val = message[0] # message[0] is voted field
                    if type(voted_val) is Value and self.check_Quorum_threshold(voted_val):

                        log.node.info('Quorum threshold met for voted value %s at Node %s', voted_val, self.name)
                        self.update_nomination_state(voted_val, "voted")

                    if type(voted_val) is Value and self.check_Blocking_threshold(voted_val):
                        log.node.info('Blocking threshold met for value %s at Node %s', voted_val, self.name)

                    accepted_val = message[1] # message[1] is accepted field
                    if type(accepted_val) is Value and self.check_Quorum_threshold(accepted_val):

                        log.node.info('Quorum threshold met for accepted value %s at Node %s', accepted_val, self.name)

                        self.update_nomination_state(accepted_val, "accepted")

                    if type(accepted_val) is Value and self.check_Blocking_threshold(accepted_val):
                        log.node.info('Blocking threshold met for value %s at Node %s', accepted_val, self.name)

                else:
                    log.node.info('Node %s has no messages to retrieve from his highest priority neighbor Node %s!', self.name, priority_node.name)

    def update_local_nomination_broadcast(self):
        """
        Constructs a new SCPNominate message using the current nomination state
        (voted and accepted) and replaces the node's broadcast flags with only that message.
        """
        voted_vals = self.nomination_state.get('voted', [])
        accepted_vals = self.nomination_state.get('accepted', [])
        confirmed_vals = self.nomination_state.get('confirmed', [])
        if not voted_vals and not accepted_vals:
            log.node.info("Node %s has empty nomination state; not updating broadcast flag.", self.name)
            return
        # Create a new SCPNominate message with the latest nomination state.
        new_nom_msg = SCPNominate(voted=voted_vals, accepted=accepted_vals, confirmed=confirmed_vals)
        # Replace the broadcast flags with just this new message.
        self.broadcast_flags = [new_nom_msg]
        log.node.info("Node %s updated its local nomination broadcast flag: %s", self.name, new_nom_msg)

    def process_received_message(self, message):
        finalized_tx_ids = self.get_finalized_transaction_ids()

        # Prune any already-finalized tx from our current nomination state
        for phase in ['voted', 'accepted', 'confirmed']:
            pruned = []
            for old_val in self.nomination_state.get(phase, []):
                keep = {t for t in old_val.transactions if t.hash not in finalized_tx_ids}
                if keep:
                    pruned.append(Value(transactions=keep))
            self.nomination_state[phase] = pruned

        incoming_voted = message[0]
        if isinstance(incoming_voted, Value):
            # Prune out finalized txs
            filtered = {tx for tx in incoming_voted.transactions if tx.hash not in finalized_tx_ids}
            if filtered:
                # Merge with our existing voted Values
                current = self.nomination_state.get('voted', [])
                merged = Value.combine(current + [Value(transactions=filtered)])

                txs = sorted(merged.transactions, key=lambda tx: tx.hash)
                if len(txs) > self.MAX_SLOT_TXS:
                    capped = set(txs[: self.MAX_SLOT_TXS])
                    merged = Value(transactions=capped)
                    log.node.info(
                        "Node %s: truncating merged voted txs → %d (slot limit).",
                        self.name, self.MAX_SLOT_TXS
                    )

                self.nomination_state['voted'] = [merged]
                log.node.info(
                    "Node %s updated voted nomination state with capped merged value: %s",
                    self.name, merged
                )

        incoming_accepted = message[1]
        if isinstance(incoming_accepted, Value):
            filtered = {tx for tx in incoming_accepted.transactions if tx.hash not in finalized_tx_ids}
            if filtered:
                current = self.nomination_state.get('accepted', [])
                merged = Value.combine(current + [Value(transactions=filtered)])

                # Cap it
                txs = sorted(merged.transactions, key=lambda tx: tx.hash)
                if len(txs) > self.MAX_SLOT_TXS:
                    capped = set(txs[: self.MAX_SLOT_TXS])
                    merged = Value(transactions=capped)
                    log.node.info(
                        "Node %s: truncating merged accepted txs → %d (slot limit).",
                        self.name, self.MAX_SLOT_TXS
                    )

                self.nomination_state['accepted'] = [merged]
                log.node.info(
                    "Node %s updated accepted nomination state with capped merged value: %s",
                    self.name, merged
                )

        self.update_local_nomination_broadcast()

    def is_value_already_present(self, new_value):
        """
        Checks if a Value (new_value) is already present in any nomination state
        (voted, accepted, confirmed). Comparison is done using the hash
        (or using __eq__ if that is defined on Value).
        """
        for state in ['voted', 'accepted', 'confirmed']:
            for val in self.nomination_state.get(state, []):
                if new_value == val:  # relies on Value.__eq__ (and __hash__)
                    return True
        return False

    def clean_nomination_state_duplicates(self):
        """
        Cleans each list in nomination_state by removing duplicate Value objects.
        This function modifies nomination_state in-place.
        """
        for state in self.nomination_state:
            seen = set()
            unique_values = []
            # Iterate in order to preserve the order (if necessary).
            for value in self.nomination_state[state]:
                if value not in seen:
                    unique_values.append(value)
                    seen.add(value)
            self.nomination_state[state] = unique_values
        log.node.info("Node %s cleaned nomination_state; current state: %s", self.name, self.nomination_state)

    def clean_prepare_and_commit_state(self):
        """
        Removes entries from prepare and commit phases that contain finalized transactions.
        Ensures finalized transactions do not block nomination.
        """

        # Helper to check if a ballot's value is entirely finalized
        def is_finalized(value):
            return all(t.hash in self.finalised_transactions for t in value.transactions)

        # Clean prepare phase
        for phase in ['voted', 'accepted', 'confirmed', 'aborted']:
            self.balloting_state[phase] = {
                val_hash: ballot for val_hash, ballot in self.balloting_state[phase].items()
                if not is_finalized(ballot.value)
            }

        self.ballot_statement_counter = {
            val: counters for val, counters in self.ballot_statement_counter.items()
            if not is_finalized(val)
        }

        self.prepared_ballots = {
            val: prep for val, prep in self.prepared_ballots.items()
            if not is_finalized(val)
        }

        # Clean commit phase
        for phase in ['voted', 'accepted', 'confirmed']:
            self.commit_ballot_state[phase] = {
                val_hash: ballot for val_hash, ballot in self.commit_ballot_state[phase].items()
                if not is_finalized(ballot.value)
            }

        self.commit_ballot_statement_counter = {
            val: counters for val, counters in self.commit_ballot_statement_counter.items()
            if not is_finalized(val)
        }

        self.committed_ballots = {
            val: cmt for val, cmt in self.committed_ballots.items()
            if not is_finalized(val)
        }

    def prepare_nomination_msg(self):
        """
        Prepares an SCPNominate message by selecting up to MAX_SLOT_TXS new transactions
        using a sliding-window queue. Every seen transaction is enqueued, and each slot
        nominates at most MAX_SLOT_TXS in FIFO order, ensuring no starvation.
        """
        # Initialize tx_queue if not already present
        if not hasattr(self, 'tx_queue'):
            from collections import deque
            self.tx_queue = deque()

        all_tx = self.mempool.get_all_transactions()
        for tx in all_tx:
            if tx.hash not in self.finalised_transactions and tx not in self.tx_queue:
                self.tx_queue.append(tx)

        # Prune nomination_state of finalized txs
        self.collect_finalised_transactions()
        self.clean_prepare_and_commit_state()
        for phase in ['voted', 'accepted', 'confirmed']:
            pruned = []
            for old_val in self.nomination_state.get(phase, []):
                keep = {t for t in old_val.transactions if t.hash not in self.finalised_transactions}
                if keep:
                    pruned.append(Value(transactions=keep))
            self.nomination_state[phase] = pruned

        # Select up to MAX_SLOT_TXS from the front of the tx_queue
        to_nominate = []
        for _ in range(min(self.MAX_SLOT_TXS, len(self.tx_queue))):
            to_nominate.append(self.tx_queue.popleft())

        if not to_nominate:
            log.node.info('Node %s found no transactions to nominate after queue processing.', self.name)
            return

        log.node.critical('Node %s preparing SCPNominate message', self.name)
        # Build new Value and merge with existing 'voted'
        new_value = Value(transactions=set(to_nominate))
        if self.is_value_already_present(new_value):
            log.node.info('Node %s already has a nomination with transactions %s. Skipping.',
                          self.name, new_value.transactions)
            return

        if self.nomination_state['voted']:
            existing = self.nomination_state['voted']
            combined = Value.combine(existing + [new_value])

            if len(combined.transactions) > self.MAX_SLOT_TXS:
                sorted_txs = sorted(combined.transactions, key=lambda tx: tx.hash)
                capped = set(sorted_txs[:self.MAX_SLOT_TXS])
                combined = Value(transactions=capped)
                log.node.info(
                    'Node %s: truncating merged %d txs → %d (slot limit).',
                    self.name, len(sorted_txs), self.MAX_SLOT_TXS
                )
            self.nomination_state['voted'] = [combined]
            log.node.info('Node %s merged new txs into voted nomination: %s', self.name, combined)
        else:
            self.nomination_state['voted'] = [new_value]
            log.node.info('Node %s set its voted nomination to new value: %s', self.name, new_value)

        # Broadcast the SCPNominate message
        message = SCPNominate(
            voted=self.nomination_state['voted'],
            accepted=self.nomination_state['accepted'],
            confirmed=self.nomination_state['confirmed'],
        )
        self.storage.add_messages(message)
        self.broadcast_flags = [message]
        log.node.info('Node %s prepared SCPNominate message: %s', self.name, message)
        return message

    def get_messages(self):
        if len(self.storage.messages) == 0:
            messages = None
            log.node.info('Node %s: No messages to retrieve!',self.name)
        else:

            messages = self.storage.messages.copy()
        return messages

    def update_statement_count(self, other_node, message):
        raw_voted, raw_accepted = message

        # Normalize to lists
        if isinstance(raw_voted, Value):
            voted_vals = [raw_voted]
        elif isinstance(raw_voted, list):
            voted_vals = raw_voted
        else:
            voted_vals = []

        if isinstance(raw_accepted, Value):
            accepted_vals = [raw_accepted]
        elif isinstance(raw_accepted, list):
            accepted_vals = raw_accepted
        else:
            accepted_vals = []

        # First handle all the accepted nominations
        for val in accepted_vals:
            h = val.hash
            # Ensure an entry in the counter
            if h not in self.statement_counter:
                self.statement_counter[h] = {"voted": {}, "accepted": {}}
            # If we haven't recorded other_node's accept yet, record it
            if other_node.name not in self.statement_counter[h]["accepted"]:
                self.statement_counter[h]["accepted"][other_node.name] = 1
                log.node.info(
                    "Node %s recorded ACCEPT from %s for value %s",
                    self.name, other_node.name, h
                )

        # Now handle all the voted nominations
        for val in voted_vals:
            h = val.hash
            # Ensure an entry in the counter
            if h not in self.statement_counter:
                self.statement_counter[h] = {"voted": {}, "accepted": {}}
            # If we haven't recorded other_node's vote yet, record it
            if other_node.name not in self.statement_counter[h]["voted"]:
                self.statement_counter[h]["voted"][other_node.name] = 1
                log.node.info(
                    "Node %s recorded VOTE from %s for value %s",
                    self.name, other_node.name, h
                )

    # - Let "Gi(m) = SHA-256(i || m)", where "||" denotes the
    #   concatenation of serialized XDR values.  Treat the output of "Gi"
    #   as a 256-bit binary number in big-endian format.
    def Gi(self, values):
        """
        Computes the Gi function: SHA-256(i || m) as per SCP specifications.
        """
        # Ensure values is always a list
        if not isinstance(values, list):
            values = [values]

        # Concatenate XDR serialized values
        packer = xdrlib3.Packer()
        packer.pack_int(self.slot)

        for value in values:
            if isinstance(value, str):
                packer.pack_bytes(value.encode('utf-8'))
            elif isinstance(value, int):
                packer.pack_int(value)
            elif isinstance(value, bytes):
                packer.pack_bytes(value)
            elif hasattr(value, "serialize_xdr"):
                packer.pack_bytes(value.serialize_xdr())
            else:
                raise TypeError(f"Unsupported value type in Gi: {type(value)}")

        packed_data = packer.get_buffer()

        # Compute SHA-256 hash and interpret as a big-endian integer
        hash_bytes = hashlib.sha256(packed_data).digest()

        # Convert the hash bytes to a big-endian integer
        return int.from_bytes(hash_bytes, byteorder='big')

    # - For each peer "v", define "weight(v)" as the fraction of quorum
    #   slices containing "v".
    def weight(self,v):
        return 1.0

    # - Define the set of nodes "neighbors(n)" as the set of nodes v for
    #   which "Gi(1 || n || v) < 2^{256} * weight(v)", where "1" and "n"
    #   are both 32-bit XDR "int" values.  Note that a node is always its
    #   own neighbor because conceptually a node belongs to all of its own
    #   quorum slices.

    # Because Gi(1 || n || v) is a random function with a maximum value of 2^{256}, this formula effectivelly
    # selects a peer as a neighbor with a probability equal to its weight!

    def _flatten_nested(self, q):
        """
        Recursively descend into `q`, which may be:
          - a Node
          - a list of Node
          - a list of lists (and so on)
        Return a flat Python list of all Node objects found inside.
        """
        flat = []
        for x in q:
            if isinstance(x, list):
                flat.extend(self._flatten_nested(x))
            else:
                # we assume anything not a list is a Node instance
                flat.append(x)
        return flat

    def get_priority_list(self):
        print(f"Nodes in quorum set: {self.quorum_set.get_nodes()}")
        print(f"Inner sets in quorum set: {self.quorum_set.get_inner_sets()}")

        unique_nodes = set()

        # Always allow “self” if hash‐prng gives it priority
        if self.Gi([1, self.nomination_round, str(self.name)]) < (2 ** 256 * 1.0):
            print("SELF WAS ADDED")
            unique_nodes.add(self)

        # Check each top‐level validator
        for node in self.quorum_set.get_nodes():
            if self.Gi([1, self.nomination_round, str(node.name)]) < (
                    2 ** 256 * self.quorum_set.weight(node)):
                unique_nodes.add(node)

        # Now descend into all nested inner‐sets (any depth)
        for inner_set in self.quorum_set.get_inner_sets():
            # `inner_set` might be a list of Node or a list of lists…
            all_nodes = self._flatten_nested(inner_set)
            for node in all_nodes:
                if self.Gi([1, self.nomination_round, str(node.name)]) < (
                        2 ** 256 * self.quorum_set.weight(node)):
                    unique_nodes.add(node)

        # Update and return
        self.priority_list.update(unique_nodes)
        return unique_nodes

    # - Define "priority(n, v)" as "Gi(2 || n || v)", where "2" and "n"
    #   are both 32-bit XDR "int" values.
    def priority(self,v):
        return self.Gi([2,self.nomination_round,v.name])

    def get_highest_priority_neighbor(self):
        # Update nomination round and priority list as needed.
        self.check_update_nomination_round()
        neighbors = self.get_priority_list()

        if not neighbors:
            log.node.warning('Node %s has no nodes in the priority list!', self.name)
            print("Nodes in quorum set:", self.quorum_set.get_nodes(), self.quorum_set.get_inner_sets())
            return None

        available_neighbors = [node for node in neighbors if node != self]
        if not available_neighbors:
            available_neighbors = list(neighbors)

        # Choose the node with the lowest Gi value – assuming lower Gi means higher priority.
        highest_priority_neighbor = max(available_neighbors,
                                        key=lambda neighbor: self.priority(neighbor))

        log.node.info('Node %s has highest priority neighbor: %s', self.name, highest_priority_neighbor.name)
        return highest_priority_neighbor

    def is_duplicate_value(self, other_val, current_vals):
        for val in current_vals:
            if other_val == val:
                return True
        return False

    def check_Quorum_threshold(self, val):
        if val not in self.nomination_state["voted"] and val not in self.nomination_state["accepted"]:
            # make sure Node itself has voted or accepted message before checking quorum
            return False

        # get all peers
        top = self.quorum_set.get_nodes()
        inner = self.quorum_set.get_inner_sets()

        all_peers = list(top) + self._flatten_nested(inner)

        seen_ids = set()
        unique = []
        for p in all_peers + [self]: # remove possible duplicate nodes
            if id(p) not in seen_ids:
                seen_ids.add(id(p))
                unique.append(p)

        entry = self.statement_counter.get(val.hash, {"voted": set(), "accepted": set()})
        signed = sum( 1 for p in unique if p is self or p.name in entry["voted"] or p.name in entry["accepted"]) # count signatures

        needed = self.quorum_set.minimum_quorum
        log.node.debug("Nomination quorum check at Node %s for Value %s: signed=%d, needed=%d", self.name, val, signed, needed )
        return signed >= needed # return count vs. threshold

    def check_Blocking_threshold(self, val):

        if val not in self.nomination_state["voted"] and val not in self.nomination_state["accepted"]:
            return False

        validators, inner_sets = self.quorum_set.get_quorum()

        def _flatten(q):
            flat = []
            for x in q:
                if isinstance(x, list):
                    flat.extend(_flatten(x))
                else:
                    flat.append(x)
            return flat

        all_quorum_nodes = list(validators) + _flatten(inner_sets)

        unique_quorum_nodes = []
        seen_ids = set()
        for node in all_quorum_nodes:
            if id(node) not in seen_ids:
                seen_ids.add(id(node))
                unique_quorum_nodes.append(node)
        n = len(unique_quorum_nodes)

        k = self.quorum_set.minimum_quorum # this is the threshold

        if n == 0:
            return False

        signed_count = 1  # start with “1” because this node itself has signed (by step #1)
        seen_signed = set()  # to avoid double‐counting the same Node
        for node in unique_quorum_nodes:
            if node is self:
                continue
            # check if that node has “voted” or “accepted” in statement_counter for this value
            voted_set = self.statement_counter[val.hash]["voted"]
            accepted_set = self.statement_counter[val.hash]["accepted"]
            if (node in voted_set or node in accepted_set) and node not in seen_signed:
                seen_signed.add(node)
                signed_count += 1

        inner_set_count = 0
        for raw_slice in inner_sets:
            flat_slice = _flatten([raw_slice])
            inner_set_count += self.quorum_set.check_inner_set_blocking_threshold( calling_node=self, val=val, quorum=flat_slice)

        return (signed_count + inner_set_count) > (n - k)


    def update_nomination_state(self, val, field):
        if field == "voted":
            if len(self.nomination_state["voted"]) > 0 :
                if val in self.nomination_state['accepted']:
                    log.node.info('Value %s is already accepted in Node %s', val, self.name)
                    if val in self.nomination_state['voted']:
                        self.nomination_state['voted'].remove(val)
                    return

                if val in self.nomination_state['voted']:
                    self.nomination_state['voted'].remove(val)

                self.nomination_state['accepted'].append(val)
                log.node.info('Value %s has been moved to accepted in Node %s', val, self.name)
            else:
                log.node.info('No values in voted state, cannot move Value %s to accepted in Node %s', val, self.name)

        elif field == "accepted":
            if len(self.nomination_state["accepted"]) > 0:
                if val in self.nomination_state['confirmed']:
                    log.node.info('Value %s is already confirmed in Node %s', val, self.name)
                    if val in self.nomination_state['accepted']:
                        self.nomination_state['accepted'].remove(val)
                    return

                if val in self.nomination_state['accepted']:
                    self.nomination_state['accepted'].remove(val)

                self.nomination_state['confirmed'].append(val)
                log.node.info('Value %s has been moved to confirmed in Node %s', val, self.name)
            else:
                log.node.info('No values in accepted state, cannot move Value %s to confirmed in Node %s', val, self.name)

    # retrieve a confirmed Value from nomination_state
    def retrieve_confirmed_value(self):
        if len(self.nomination_state['confirmed']) > 0:
            confirmed_value = np.random.choice(self.nomination_state['confirmed'])  # Take a random Value from the confirmed state
            log.node.info('Node %s retrieved confirmed value %s for SCPPrepare', self.name, confirmed_value)
            return confirmed_value
        else:
            log.node.info('Node %s has no confirmed values to use for SCPPrepare!', self.name)
            return None

    def get_prepared_ballot_counters(self, value):
        return self.prepared_ballots.get(value)

    def replace_prepare_broadcast_flag(self, new_msg: SCPPrepare):
        # find any existing messages whose .ballot is equal to new_msg.ballot
        to_remove = {m for m in self.ballot_prepare_broadcast_flags
                     if m.ballot == new_msg.ballot}
        if to_remove:
            log.node.debug("Node %s replacing old Prepare(s) %s with %s",
                           self.name, to_remove, new_msg)

        self.ballot_prepare_broadcast_flags.difference_update(to_remove)
        self.ballot_prepare_broadcast_flags.add(new_msg)

    def prepare_ballot_msg(self):
        """
        Prepare Ballot for Prepare Balloting phase
        """
        if len(self.nomination_state['confirmed']) == 0: # Check if there are any values to prepare
            log.node.info('Node %s has no confirmed values in nomination state to prepare for balloting.', self.name)
            return

        # Retrieve a Value from the Nomination 'confirmed' state
        confirmed_val = self.retrieve_confirmed_value()

        if self.is_finalized(confirmed_val):
            log.node.info('Node %s: Value to be prepared %s is already finalized, skipping SCPPrepare ballot preparation.',
                          self.name, confirmed_val)

            # Remove the finalized value from nomination confirmed state
            for state in ['confirmed', 'voted', 'accepted']:
                self.nomination_state[state] = [
                    val for val in self.nomination_state[state]
                    if val != confirmed_val
                ]
            return

        # If the value has been aborted in the past then it should not be prepared
        if confirmed_val.hash in self.balloting_state['aborted']:
            log.node.info('Node %s has aborted ballot %s previously, so it cannot be prepared.', self.name,
                          confirmed_val.hash)
            return

        if len(self.balloting_state['voted']) > 0 and confirmed_val.hash in self.balloting_state['voted']:
            # Retrieve the counter from the state and increase it by one for the new ballot to be created
            new_counter = self.balloting_state['voted'][confirmed_val.hash].counter + 1
            self.balloting_state['voted'][confirmed_val.hash].counter += 1
            ballot = SCPBallot(counter=new_counter, value=confirmed_val)
            log.node.info('Node %s created SCPBallot with larger count: %s', self.name, ballot.value)
        else:
            ballot = SCPBallot(counter=1, value=confirmed_val)
            log.node.info('Node %s created SCPBallot: %s', self.name, ballot.value)

        if not self.check_if_finalised(ballot):
            log.node.critical('Node %s created SCPBallot', self.name)
            # Get counters for new SCPPrepare message
            prepare_msg_counters = self.get_prepared_ballot_counters(confirmed_val)
            if prepare_msg_counters is not None:
                prepare_msg = SCPPrepare(ballot=ballot, aCounter=prepare_msg_counters.aCounter, cCounter=prepare_msg_counters.cCounter, hCounter=prepare_msg_counters.hCounter)
                log.node.info('Node %s has increased counter on prepared SCPPrepare message with ballot %s, h_counter=%d, a_counter=%d, c_counter=%d.',self.name, confirmed_val, prepare_msg_counters.aCounter, prepare_msg_counters.cCounter,prepare_msg_counters.hCounter)

                self.replace_prepare_broadcast_flag(prepare_msg)
                self.prepared_ballots[ballot.value] = prepare_msg
                if ballot.value not in self.ballot_statement_counter:
                    self.ballot_statement_counter[ballot.value] = {'voted': set(), 'accepted': set(), 'confirmed':set(), 'aborted':set()}
                    self.ballot_statement_counter[ballot.value]['voted'] = set()
                    self.ballot_statement_counter[ballot.value]['voted'].add(self)
                    self.ballot_statement_counter[ballot.value]['accepted'].add(self)
            else:
                # If prepare_msg_counters is none then there are no counters for this value and we have to set the defaults
                prepare_msg = SCPPrepare(ballot=ballot)
                self.replace_prepare_broadcast_flag(prepare_msg)
                self.prepared_ballots[ballot.value] = prepare_msg
                self.balloting_state['voted'][confirmed_val.hash] = ballot
                if ballot.value not in self.ballot_statement_counter:
                    self.ballot_statement_counter[ballot.value] = {'voted': set(), 'accepted': set(), 'confirmed':set(), 'aborted':set()}
                    self.ballot_statement_counter[ballot.value]['voted'] = set()
                    self.ballot_statement_counter[ballot.value]['voted'].add(self)
                    self.ballot_statement_counter[ballot.value]['accepted'].add(self)
                log.node.info('Node %s has prepared SCPPrepare message with ballot %s, h_counter=%d, a_counter=%d, c_counter=%d.', self.name, confirmed_val, 0, 0,0)

            log.node.info('Node %s appended SCPPrepare message to its storage and state, message = %s', self.name, prepare_msg)
        else:
            log.node.info('Node %s has not prepared SCPPrepare message as the ballot %s has already been finalised', self.name, ballot)

    def process_prepare_ballot_message(self, message, sender):
        received_ballot = message.ballot

        # Case 1: New ballot received has the same value but a higher counter
        if received_ballot.value.hash in self.balloting_state['voted']:
            if received_ballot.value == self.balloting_state['voted'][received_ballot.value.hash].value and received_ballot.counter > self.balloting_state['voted'][received_ballot.value.hash].counter:
                log.node.info("Node %s received a ballot with the same value but a higher counter. Updating to the new counter.", self.name)
                self.balloting_state['voted'][received_ballot.value.hash] = received_ballot
                if received_ballot.value not in self.ballot_statement_counter:
                    self.ballot_statement_counter[received_ballot.value] = {'voted': set(), 'accepted': set(), 'confirmed': set(), 'aborted': set()}
                    self.ballot_statement_counter[received_ballot.value]['voted'].add(sender)
                    self.ballot_statement_counter[received_ballot.value]['accepted'].add(sender)

                else:
                    self.ballot_statement_counter[received_ballot.value]['voted'].add(sender)
                    self.ballot_statement_counter[received_ballot.value]['accepted'].add(sender)
                return

            # Case 3: New ballot received has the same value but a lower counter
            if received_ballot.counter < self.balloting_state['voted'][received_ballot.value.hash].counter:
                log.node.info("Node %s that has been received has the same value but a lower counter than a previously voted ballot.", self.name)
                if received_ballot.value not in self.ballot_statement_counter:
                    self.ballot_statement_counter[received_ballot.value] = {'voted': set(),'accepted': set(),'confirmed': set(),'aborted': set()}
                    self.ballot_statement_counter[received_ballot.value]['voted'].add(sender)
                    self.ballot_statement_counter[received_ballot.value]['accepted'].add(sender)
                else:
                    self.ballot_statement_counter[received_ballot.value]['voted'].add(sender)
                    self.ballot_statement_counter[received_ballot.value]['accepted'].add(sender)
                return

        elif received_ballot.value.hash not in self.balloting_state['voted']:
            for voted_ballot in self.balloting_state['voted'].values():
                # Case 2: New ballot received has different value and a higher counter
                if received_ballot.counter > voted_ballot.counter:
                    log.node.info("Node %s received a ballot with a different value and a higher counter. Aborting previous ballots.",self.name)
                    # Abort any previous ballots with a smaller counter and different value
                    self.abort_ballots(received_ballot)
                    self.balloting_state['voted'][received_ballot.value.hash] = received_ballot
                    if received_ballot.value not in self.ballot_statement_counter:
                        self.ballot_statement_counter[received_ballot.value] = {'voted': set(), 'accepted': set(),'confirmed': set(), 'aborted': set()}
                        self.ballot_statement_counter[received_ballot.value]['voted'].add(sender)
                        self.ballot_statement_counter[received_ballot.value]['accepted'].add(sender)
                    else:
                        self.ballot_statement_counter[received_ballot.value]['voted'].add(sender)
                        self.ballot_statement_counter[received_ballot.value]['accepted'].add(sender)
                    return

            # Case 4: New ballot received has different value and a lower counter - JUST abort this received ballot
            self.balloting_state['aborted'][received_ballot.value.hash] = received_ballot
            if received_ballot.value not in self.ballot_statement_counter:
                self.ballot_statement_counter[received_ballot.value] = {'voted': set(), 'accepted': set(),'confirmed': set(), 'aborted': set()}
                self.ballot_statement_counter[received_ballot.value]['aborted'].add(sender)
            else:
                if sender in self.ballot_statement_counter[received_ballot.value]['voted']:
                    self.ballot_statement_counter[received_ballot.value]['voted'].remove(sender)
                self.ballot_statement_counter[received_ballot.value]['aborted'].add(sender)
            log.node.info("Node %s hreceived a ballot with a different value and a higher counter. Abortingthis ballot.")

    def abort_ballots(self, received_ballot):
        voted_ballots_to_del = []
        accepted_ballots_to_del = []

        # Abort all ballots from 'voted' field that have a lower counter
        for ballot in self.balloting_state['voted'].values():
            if ballot.counter < received_ballot.counter:
                if ballot.value.hash != received_ballot.value.hash: # every ballot less than "b" containing a value other than "b.value"
                    self.balloting_state['aborted'][ballot.value.hash] = ballot
                    voted_ballots_to_del.append(ballot.value.hash)

        for ballot in voted_ballots_to_del:
            self.balloting_state['voted'].pop(ballot)

        # Abort all Values from 'accepted' field that have a lower counter
        for ballot in self.balloting_state['accepted'].values():
            if ballot.counter < received_ballot.counter:
                if ballot.value.hash != received_ballot.value.hash:
                    self.balloting_state['aborted'][ballot.value.hash] = ballot
                    accepted_ballots_to_del.append(ballot.value.hash)

        for ballot in accepted_ballots_to_del:
            self.balloting_state['accepted'].pop(ballot)

    def check_Prepare_Quorum_threshold(self, ballot):
        val_hash = ballot.value.hash

        if val_hash not in self.balloting_state["voted"] and \
                val_hash not in self.balloting_state["accepted"]:
            return False

        validators, inner_sets = self.quorum_set.get_quorum()
        all_quorum = list(validators) + self._flatten_nested(inner_sets)

        entry = self.ballot_statement_counter.get(ballot.value, {"voted": set(), "accepted": set()})
        signed = 1
        for n in all_quorum:
            if n is not self and (n in entry["voted"] or n in entry["accepted"]):
                signed += 1

        needed = self.quorum_set.minimum_quorum
        log.node.debug( "Prepare quorum check at Node %s for ballot %s: signed=%d, needed=%d", self.name, ballot, signed, needed)

        return signed >= needed

    def update_prepare_balloting_state(self, ballot, field):
        if field == "voted":
            if len(self.balloting_state["voted"]) > 0 :
                if ballot.value.hash in self.balloting_state['accepted']:
                    log.node.info('Value %s is already accepted for SCPPrepare phase in Node %s', ballot.value, self.name)
                    return

                if ballot.value.hash in self.balloting_state['voted']:
                    self.balloting_state["accepted"][ballot.value.hash] = (self.balloting_state["voted"][ballot.value.hash])
                    self.balloting_state["voted"].pop(ballot.value.hash)
                    log.node.info('Ballot %s has been moved to accepted for SCPPrepare phase in Node %s', ballot, self.name)
            else:
                log.node.info('No ballots in voted state, cannot move Ballot %s to accepted in Node %s', ballot, self.name)

        elif field == "accepted":
            if len(self.balloting_state["accepted"]) > 0:
                if ballot.value.hash in self.balloting_state['confirmed']:
                    log.node.info('Ballot %s is already confirmed for SCPPrepare phase in Node %s', ballot, self.name)
                    return

                if ballot.value.hash in self.balloting_state['accepted']:
                    self.balloting_state["confirmed"][ballot.value.hash] = (self.balloting_state["accepted"][ballot.value.hash])
                    self.balloting_state["accepted"].pop(ballot.value.hash)

                log.node.info('Ballot %s has been moved to confirmed for SCPPrepare phase in Node %s', ballot.value.hash, self.name)
            else:
                log.node.info('No ballots in accepted state, cannot move Ballots %s for SCPPrepare phase to confirmed in Node %s', ballot, self.name)


    def retrieve_ballot_prepare_message(self, sending_node):
        # Select a random ballot and check if its already been sent to the requesting_node
        if len(sending_node.ballot_prepare_broadcast_flags) > 0:
            if sending_node.name not in self.received_prepare_broadcast_msgs:
                retrieved_message = np.random.choice(list(sending_node.ballot_prepare_broadcast_flags))
                if self.check_if_finalised(retrieved_message.ballot):
                    log.node.info(
                            'Node %s: Value in Ballot %s is already finalized, skipping SCPCommit preparation.',
                            self.name, retrieved_message.ballot.value)

                    return None

                self.received_prepare_broadcast_msgs[sending_node.name] = [retrieved_message]
                return retrieved_message

            else:
                    retrieved_message = np.random.choice(list(sending_node.ballot_prepare_broadcast_flags))
                    if retrieved_message not in self.received_prepare_broadcast_msgs[sending_node.name]:
                        self.received_prepare_broadcast_msgs[sending_node.name].append(retrieved_message)
                        return retrieved_message
        return None


    def is_v_blocking(self, other_ballot):
        validators, inner_sets = self.quorum_set.get_quorum()
        all_quorum = list(validators) + self._flatten_nested(inner_sets)

        unique_peers = {id(n) for n in all_quorum}
        n = len(unique_peers)

        k = self.quorum_set.minimum_quorum
        threshold = n - k

        entry = self.ballot_statement_counter.get(other_ballot.value, {"voted": set(), "accepted": set()})
        count = len(entry["voted"] | entry["accepted"])
        return count > threshold

    def receive_prepare_message(self):
        peer = self.quorum_set.retrieve_random_peer(self)
        if peer is None or peer is self:
            log.node.info('Node %s: no valid peer for prepare messages', self.name)
            return

        existing = self.received_prepare_broadcast_msgs.get(peer.name)
        if not isinstance(existing, set):
            self.received_prepare_broadcast_msgs[peer.name] = set()
        seen = self.received_prepare_broadcast_msgs[peer.name]

        unseen = [msg for msg in peer.ballot_prepare_broadcast_flags if msg not in seen]
        if not unseen:
            log.node.info('Node %s: no new prepare messages from %s', self.name, peer.name)
            return
        log.node.critical('Node %s processing SCPPrepare messages %s', self.name)
        for msg in unseen:
            seen.add(msg)

            if self.check_if_finalised(msg.ballot):
                log.node.info('Node %s: skipping finalized prepare %s from %s',
                              self.name, msg.ballot, peer.name)
                continue

            self.process_prepare_ballot_message(msg, peer)
            log.node.info('Node %s retrieved prepare from %s: %s',
                          self.name, peer.name, msg.ballot)

            b = msg.ballot
            is_ballot = isinstance(b, SCPBallot)
            b_hash = b.value.hash

            if is_ballot and b_hash in self.balloting_state['voted'] \
                    and self.check_Prepare_Quorum_threshold(b):
                log.node.info('Node %s: quorum met for voted %s', self.name, b)
                self.update_prepare_balloting_state(b, "voted")

            elif is_ballot and b_hash in self.balloting_state['accepted'] \
                    and self.check_Prepare_Quorum_threshold(b):
                log.node.info('Node %s: quorum met for accepted %s', self.name, b)
                self.update_prepare_balloting_state(b, "accepted")

            # 4b) Blocking‐threshold check
            for old_hash, old_ballot in list(self.balloting_state['voted'].items()):
                if old_hash != b_hash and self.is_v_blocking(b):
                    log.node.info('Node %s: %s v-blocks %s → aborting %s',
                                  self.name, b, old_ballot, old_ballot)
                    self.abort_ballots(b)
                    # ensure you vote for the blocking ballot if not already
                    if b_hash not in self.balloting_state['voted']:
                        self.balloting_state['voted'][b_hash] = b
                    break


    def retrieve_confirmed_prepare_ballot(self):
        if len(self.balloting_state['confirmed']) > 0:
            random_ballot_hash = np.random.choice(list(self.balloting_state['confirmed'].keys()))
            confirmed_prepare_ballot = self.balloting_state['confirmed'][random_ballot_hash]  # Take a random Value from the confirmed state
            log.node.info('Node %s retrieved confirmed prepared ballot %s for SCPCommit', self.name, confirmed_prepare_ballot)
            return confirmed_prepare_ballot
        else:
            log.node.info('Node %s has no confirmed prepared ballots to use for SCPCommit!', self.name)
            return None


    def prepare_SCPCommit_msg(self):
        """
        Prepare SCPCommit message for Commit Ballot phase
        """
        if len(self.balloting_state['confirmed']) == 0: # Check if there are any values to prepare
            log.node.info('Node %s has no confirmed values in nomination state to prepare for balloting.', self.name)
            return

        confirmed_ballot = self.retrieve_confirmed_prepare_ballot() # Retrieve a Value from the SCPPrepare 'confirmed' state
        if confirmed_ballot is not None:
            if self.is_ballot_finalized(confirmed_ballot):
                log.node.info('Node %s: Value in Ballot %s is already finalized, skipping SCPCommit preparation.', self.name,
                              confirmed_ballot.value)

                # Remove the finalized ballot from all balloting states
                self.reset_prepare_ballot_phase(confirmed_ballot)
                self.reset_commit_phase_state(confirmed_ballot)
                return

            commit_msg = SCPCommit(ballot=confirmed_ballot, preparedCounter=confirmed_ballot.counter)
            self.commit_ballot_broadcast_flags.add(commit_msg)
            self.commit_ballot_state['voted'][confirmed_ballot.value.hash] = confirmed_ballot
            if confirmed_ballot.value not in self.commit_ballot_statement_counter:
                    self.commit_ballot_statement_counter[confirmed_ballot.value] = {'voted': set(), 'accepted': set(), 'confirmed':set(), 'aborted':set()}
                    self.commit_ballot_statement_counter[confirmed_ballot.value]['voted'] = set()
                    self.commit_ballot_statement_counter[confirmed_ballot.value]['voted'].add(self)
            log.node.info('Node %s has prepared SCPCommit message with ballot %s, preparedCounter=%d.', self.name, confirmed_ballot, confirmed_ballot.counter)
            log.node.info('Node %s appended SCPPrepare message to its storage and state, message = %s', self.name, commit_msg)

            log.node.critical('Node %s prepared and appended SCPCommit message message %s', self.name, commit_msg)
        log.node.info('Node %s could not retrieve a confirmed SCPPrepare messages from its peer!')



    def simple_process_commit_ballot_message(self, message, sender):
        """
        Simplified commit handler: just record the received ballot and track the sender.
        """
        received = message.ballot
        h = received.value.hash

        # Initialize state containers if necessary
        if h not in self.commit_ballot_state['voted']:
            self.commit_ballot_state['voted'][h] = received

        # Track senders for this ballot
        if received.value not in self.commit_ballot_statement_counter:
            self.commit_ballot_statement_counter[received.value] = {
                'voted': set(),
                'accepted': set(),
                'confirmed': set(),
                'aborted': set()
            }
        self.commit_ballot_statement_counter[received.value]['voted'].add(sender)

        # Log receipt
        log.node.info("Node %s recorded commit ballot for %s from %s", self.name, h, sender)

    def check_Commit_Quorum_threshold(self, ballot):
        h = ballot.value.hash

        if h not in self.commit_ballot_state["voted"] and h not in self.commit_ballot_state["accepted"]:
            return False

        validators, inner_sets = self.quorum_set.get_quorum()

        def _flatten(q):
            flat = []
            for x in q:
                if isinstance(x, list):
                    flat.extend(_flatten(x))
                else:
                    flat.append(x)
            return flat

        all_peers = list(validators) + _flatten(inner_sets) + [self]
        seen_ids = set()
        unique_peers = []
        for node in all_peers:
            if id(node) not in seen_ids:
                seen_ids.add(id(node))
                unique_peers.append(node)

        entry = self.commit_ballot_statement_counter.get(ballot.value, {"voted": set(), "accepted": set()})
        signed = 0
        for node in unique_peers:
            if node is self or node in entry["voted"] or node in entry["accepted"]:
                signed += 1

        needed = self.quorum_set.minimum_quorum
        log.node.debug("Commit quorum check at Node %s for ballot %s: signed=%d, needed=%d", self.name, ballot, signed, needed)

        return signed >= needed

    def update_commit_balloting_state(self, ballot, field):
        if field == "voted":
            if len(self.commit_ballot_state["voted"]) > 0 :
                if ballot.value.hash in self.commit_ballot_state['accepted']:
                    log.node.info('Commit ballot %s is already accepted in Node %s', ballot.value, self.name)
                    return

                if ballot.value.hash in self.commit_ballot_state['voted']:
                    self.commit_ballot_state["accepted"][ballot.value.hash] = (self.commit_ballot_state["voted"][ballot.value.hash])
                    self.commit_ballot_state["voted"].pop(ballot.value.hash)
                    log.node.info('Commit ballot %s has been moved to accepted in Node %s', ballot, self.name)
            else:
                log.node.info('No commit ballots in voted state, cannot move Ballot %s to accepted in Node %s', ballot, self.name)

        elif field == "accepted":
            if len(self.commit_ballot_state["accepted"]) > 0:
                if ballot.value.hash in self.commit_ballot_state['confirmed']:
                    log.node.info('Commit ballot %s is already confirmed in Node %s', ballot, self.name)
                    return

                if ballot.value.hash in self.commit_ballot_state['accepted']:
                    self.commit_ballot_state["confirmed"][ballot.value.hash] = (self.commit_ballot_state["accepted"][ballot.value.hash])
                    self.commit_ballot_state["accepted"].pop(ballot.value.hash)

                log.node.info('Commit ballot %s has been moved to confirmed in Node %s', ballot.value.hash, self.name)
            else:
                log.node.info('No commit ballots in accepted state, cannot move Ballots %s to confirmed in Node %s', ballot, self.name)


    def retrieve_ballot_commit_message(self, sending_node):
        # Check if there are any broadcast flags
        if len(sending_node.commit_ballot_broadcast_flags) > 0:
            if sending_node.name not in self.received_commit_ballot_broadcast_msgs:
                retrieved_message = np.random.choice(list(sending_node.commit_ballot_broadcast_flags))
                self.received_commit_ballot_broadcast_msgs[sending_node.name] = [retrieved_message]
                return retrieved_message

            already_sent = self.received_commit_ballot_broadcast_msgs[sending_node.name]
            if len(already_sent) < len(sending_node.commit_ballot_broadcast_flags):
                # Choose a random message not yet sent
                remaining_messages = list(set(sending_node.commit_ballot_broadcast_flags) - set(already_sent))
                retrieved_message = np.random.choice(remaining_messages)
                self.received_commit_ballot_broadcast_msgs[sending_node.name].append(retrieved_message)
                return retrieved_message

        return None

    def _is_v_blocking_commit(self, ballot: SCPBallot) -> bool:
        entry = self.commit_ballot_statement_counter.get(
            ballot.value, {"voted": set(), "accepted": set()}
        )

        validators, inner_sets = self.quorum_set.get_quorum()

        def _flatten(q):
            flat = []
            for x in q:
                if isinstance(x, list):
                    flat.extend(_flatten(x))
                else:
                    flat.append(x)
            return flat

        all_peers = list(validators) + _flatten(inner_sets)

        seen_ids = set()
        unique_peers = []
        for p in all_peers: # remove duplicates
            if id(p) not in seen_ids:
                seen_ids.add(id(p))
                unique_peers.append(p)

        n = len(unique_peers)
        k = self.quorum_set.minimum_quorum

        signed = sum( 1 for p in unique_peers if (p in entry["voted"] or p in entry["accepted"]))

        return signed > (n - k)

    def receive_commit_message(self):
        peer = self.quorum_set.retrieve_random_peer(self)
        if peer is None or peer is self:
            log.node.info('Node %s: no valid peer for commit', self.name)
            return

        existing = self.received_commit_ballot_broadcast_msgs.get(peer.name)
        if not isinstance(existing, set):
            self.received_commit_ballot_broadcast_msgs[peer.name] = set()
        seen = self.received_commit_ballot_broadcast_msgs[peer.name]

        unseen = [msg for msg in peer.commit_ballot_broadcast_flags if msg not in seen]
        if not unseen:
            log.node.info('Node %s: no new commit messages from %s', self.name, peer.name)
            return

        for msg in unseen:
            log.node.critical('Node %s retrieved SCPCommit message %s from %s', self.name, msg, peer.name)
            seen.add(msg)

            b = msg.ballot
            log.node.info('Node %s retrieved commit %s from %s', self.name, b, peer.name)

            if self.is_ballot_finalized(b):
                self.reset_prepare_ballot_phase(b)
                self.reset_commit_phase_state(b)
                continue

            self.simple_process_commit_ballot_message(msg, peer)

            bh = b.value.hash
            if bh in self.commit_ballot_state['accepted'] and self.check_Commit_Quorum_threshold(b):
                log.node.info('Node %s: quorum met for accepted commit %s', self.name, b)
                self.update_commit_balloting_state(b, "accepted")
                self.prepare_Externalize_msg()
            elif bh in self.commit_ballot_state['voted'] and self.check_Commit_Quorum_threshold(b):
                log.node.info('Node %s: quorum met for voted commit %s', self.name, b)
                self.update_commit_balloting_state(b, "voted")

            for old_hash, old_ballot in list(self.commit_ballot_state['voted'].items()):
                if old_hash != bh and self._is_v_blocking_commit(b):
                    log.node.info('Node %s: %s v-blocks %s → aborting', self.name, b, old_ballot)
                    self.reset_commit_phase_state(old_ballot)
                    # ensure you vote for the stronger ballot if not already
                    if bh not in self.commit_ballot_state['voted']:
                        self.commit_ballot_state['voted'][bh] = b
                    break


    def retrieve_confirmed_commit_ballot(self):
        if len(self.commit_ballot_state['confirmed']) > 0:
            random_ballot_hash = np.random.choice(list(self.commit_ballot_state['confirmed'].keys()))
            confirmed_commit_ballot = self.commit_ballot_state['confirmed'][random_ballot_hash]  # Take a random Value from the confirmed state
            log.node.info('Node %s retrieved confirmed commit ballot %s for SCPExternalize', self.name, confirmed_commit_ballot)
            return confirmed_commit_ballot
        else:
            log.node.info('Node %s has no confirmed commit ballots to use for SCPExternalize!', self.name)
            return None

    def check_if_finalised(self, ballot):
        for externalize_msg in self.externalized_slot_counter:
            if externalize_msg.ballot == ballot:
                return True
        return False


    def prepare_Externalize_msg(self):

        if len(self.commit_ballot_state['confirmed']) == 0: # Check if there are any values to prepare
            log.node.info('Node %s has no committed ballots to externalize.', self.name)
            return

        finalised_ballot = self.retrieve_confirmed_commit_ballot() # Retrieve a Value from the SCPPrepare 'confirmed' state
        if finalised_ballot is not None:
            externalize_msg = SCPExternalize(ballot=finalised_ballot, hCounter=finalised_ballot.counter, timestamp=Globals.simulation_time)
            temp_value = copy.deepcopy(externalize_msg.ballot.value)
            # Store the externalized value in the ledger
            self.ledger.add_slot(self.slot, externalize_msg)
            # self.externalize_broadcast_flags.add(externalize_msg)
            self.externalize_broadcast_flags.add((self.slot, externalize_msg))
            self.externalized_slot_counter.add(externalize_msg)
            log.node.info('Node %s appended SCPExternalize message for slot %d to its storage and state, message = %s', self.name, self.slot, externalize_msg)

            log.node.critical(
                'Node %s appended SCPExternalize message for slot %d to its storage and state, message = %s', self.name,
                self.slot, externalize_msg)
            # save to log file
            self.log_to_file(f"NODE - INFO - Node {self.name} appended SCPExternalize message for slot {self.slot} to its storage and state, message = {externalize_msg}")

            # Reset Nomination/Balloting data structures for next slot
            # FULL reset of nomination, not just pruning
            self.reset_nomination_state()
            self.priority_list.clear()

            self.last_nomination_start_time = Globals.simulation_time
            self.reset_commit_phase_state(externalize_msg.ballot)
            self.reset_prepare_ballot_phase(externalize_msg.ballot)

            print("Temp Value is ", temp_value)
            self.remove_txs_from_mempool(temp_value)

            self.slot += 1
            self.nomination_round = 1

        log.node.info('Node %s could not retrieve a confirmed SCPCommit message from its peer!')


    def retrieve_externalize_msg(self, requesting_node):
        # Check if there are any broadcast flags
        if len(requesting_node.externalize_broadcast_flags) > 0:
            retrieved_slot, retrieved_message = random.choice(list(requesting_node.externalize_broadcast_flags))

            if requesting_node.name not in self.peer_externalised_statements:
                self.peer_externalised_statements[requesting_node.name] = set()
                self.peer_externalised_statements[requesting_node.name].add((retrieved_slot, retrieved_message))
            else:
                self.peer_externalised_statements[requesting_node.name].add((retrieved_slot, retrieved_message))
            return retrieved_slot, retrieved_message
        return None

    def receive_Externalize_msg(self):
        # Retrieve a random externalize message from a random peer.
        sending_node = self.quorum_set.retrieve_random_peer(self)
        if sending_node is not None and sending_node != self:
            result = self.retrieve_externalize_msg(sending_node)  # expected to return (slot_number, message)
            if result is not None:
                slot_number, message = result
                self.process_externalize_msg(slot_number, message, sending_node)
            else:
                log.node.info('Node %s has no SCPExternalize messages to retrieve from peer node %s!',
                              self.name, sending_node.name)
        else:
            log.node.info('Node %s could not retrieve a valid peer for externalize messages!', self.name)

    def process_externalize_msg(self, slot_number, message, sending_node):
        # Check if the slot has already been finalized in the ledger.
        if slot_number in self.ledger.slots:
            log.node.info('Node %s already externalized slot %d. Ignoring message from %s.',
                          self.name, slot_number, sending_node.name)
            return

        if slot_number != self.slot:
            log.node.info(
                "Node %s: ignoring externalize for slot %d while at slot %d",
                self.name, slot_number, self.slot
            )
            return

        # Adopt the externalized value.
        log.node.info(f'Node {self.name}  adopting externalized value for slot {slot_number}: {message.ballot.value}', self.name, slot_number, message.ballot.value)
        log.node.critical("Node %s received an adopted externalise message for slot %s", self.name, slot_number)

        self.log_to_file(f"Node {self.name}  adopting externalized value for slot {slot_number}: {message.ballot.value}")
        self.ledger.add_slot(slot_number, message)

        # Update peer externalized statements.
        self.peer_externalised_statements.setdefault(sending_node.name, set()).add((slot_number, message))
        self.externalize_broadcast_flags.add((slot_number, message))
        self.externalized_slot_counter.add(message)

        self.nomination_round = 1

        # FULL reset of nomination, not just pruning
        self.reset_nomination_state()
        self.last_nomination_start_time = Globals.simulation_time
        self.reset_commit_phase_state(message.ballot)
        self.reset_prepare_ballot_phase(message.ballot)

        # REMOVE TXS FROM MEMPOOL
        self.remove_txs_from_mempool(message.ballot.value)

        self.slot += 1

        log.node.info('Node %s has finalized slot %d with value %s', self.name, slot_number, message.ballot.value)

    def reset_nomination_state(self):
        """
        Completely clear out any prior nomination state so that
        the next slot starts fresh.
        """
        self.nomination_state['voted'].clear()
        self.nomination_state['accepted'].clear()
        self.nomination_state['confirmed'].clear()
        self.nomination_round = 1

    def reset_commit_phase_state(self, finalized_ballot):
        # Build a set of finalized transaction hashes from the finalized ballot.
        finalized_tx_ids = {tx.hash for tx in finalized_ballot.value.transactions}

        def ballot_contains_finalized_tx(ballot):
            return any(tx.hash in finalized_tx_ids for tx in ballot.value.transactions)

        def value_contains_finalized_tx(value):
            return any(tx.hash in finalized_tx_ids for tx in value.transactions)

        self.commit_ballot_statement_counter = {
            value: data
            for value, data in self.commit_ballot_statement_counter.items()
            if not value_contains_finalized_tx(value)
        }

        for state in ['voted', 'accepted', 'confirmed']:
            self.commit_ballot_state[state] = {
                key: ballot
                for key, ballot in self.commit_ballot_state[state].items()
                if not ballot_contains_finalized_tx(ballot)
            }

        self.commit_ballot_broadcast_flags = {
            msg for msg in self.commit_ballot_broadcast_flags
            if not ballot_contains_finalized_tx(msg.ballot)
        }

        for node_name in list(self.received_commit_ballot_broadcast_msgs.keys()):
            self.received_commit_ballot_broadcast_msgs[node_name] = [
                msg for msg in self.received_commit_ballot_broadcast_msgs[node_name]
                if not ballot_contains_finalized_tx(msg.ballot)
            ]

        log.node.info("Cleared commit-phase state for ballots containing any finalized transaction at Node %s", self.name)

    def reset_prepare_ballot_phase(self, finalized_ballot):
        finalized_value_hash = finalized_ballot.value.hash

        # 1. Remove entries from balloting_state for ballots whose value matches the finalized value
        for state in ['voted', 'accepted', 'confirmed', 'aborted']:
            self.balloting_state[state] = {
                key: ballot
                for key, ballot in self.balloting_state[state].items()
                if ballot.value.hash != finalized_value_hash
            }

        # 2. Remove entries from the ballot statement counter for ballots with the finalized value
        self.ballot_statement_counter = {
            value: data
            for value, data in self.ballot_statement_counter.items()
            if value.hash != finalized_value_hash
        }

        # 3. Remove entries from prepared_ballots for ballots with the finalized value
        self.prepared_ballots = {
            value: ballot
            for value, ballot in self.prepared_ballots.items()
            if value.hash != finalized_value_hash
        }

        # 4. Remove prepare broadcast messages for ballots with the finalized value
        self.ballot_prepare_broadcast_flags = {
            msg for msg in self.ballot_prepare_broadcast_flags
            if msg.ballot.value.hash != finalized_value_hash
        }

        # 5. Remove outdated prepare ballot messages from received prepare broadcast messages
        for node_name in list(self.received_prepare_broadcast_msgs.keys()):
            self.received_prepare_broadcast_msgs[node_name] = [
                msg for msg in self.received_prepare_broadcast_msgs[node_name]
                if msg.ballot.value.hash != finalized_value_hash
            ]

        log.node.info("Cleared prepare-ballot-phase state for ballots with value hash %s at Node %s",
                      finalized_value_hash, self.name)

    def is_finalized(self, value):
        for slot_data in self.ledger.slots.values():
            finalized_value = slot_data['value']

            # Compare each transaction in the new value against finalized transactions
            for transaction in value.transactions:
                for finalized_transaction in finalized_value.transactions:
                    if transaction.hash == finalized_transaction.hash:
                        return True  # A matching transaction was found

        return False

    def is_ballot_finalized(self, ballot):
        """
        Checks if a given ballot's value has already been finalized.
        This is done by checking whether any transaction in the ballot's value
        matches any transaction in a finalized value.
        Returns True if a matching transaction is found, otherwise False.
        """
        return self.is_finalized(ballot.value)

    def remove_txs_from_mempool(self, value):
        """
        Removes all transactions contained in the given value from the mempool.
        This is called after externalization to ensure that finalized transactions are not reprocessed.
        """
        self.collect_finalised_transactions()

        for tx in self.finalised_transactions:
            try:
                self.mempool.transactions.remove(tx)
                log.mempool.info(
                    "Removed finalized tx %s from mempool for Node %s.", tx, self.name
                )
            except ValueError:
                pass

        for tx in value.transactions:
            try:
                self.mempool.transactions.remove(tx)
                log.mempool.info('Removed transaction %s from mempool for Node %s.', tx, self.name)
            except ValueError:
                # Transaction might not be present; that's fine.
                log.mempool.info('Transaction %s was not found in mempool for Node %s.', tx, self.name)

    def remove_all_finalized_nomination_transactions(self):
        """
        Removes from the nomination state (keys: 'voted', 'accepted', 'confirmed')
        any transaction that appears in the ledger as finalized.
        """
        # Collect finalized transaction hashes from ledger
        finalized_hashes = set()
        for slot, ext_msg in self.ledger.slots.items():

            if isinstance(ext_msg, dict) and "ballot" in ext_msg:
                ballot = ext_msg["ballot"]
                # Type checks just in case
                if hasattr(ballot, 'value'):
                    value = ballot.value
                    if hasattr(value, 'transactions'):
                        for tx in value.transactions:
                            finalized_hashes.add(tx.hash)
                elif isinstance(ballot, dict) and "value" in ballot:
                    value = ballot["value"]
                    if isinstance(value, dict) and "transactions" in value:
                        for tx in value["transactions"]:
                            finalized_hashes.add(getattr(tx, 'hash', tx))
        log.node.info("Finalized transaction hashes: %s", finalized_hashes)

        # Process each nomination state
        for key in ['voted', 'accepted', 'confirmed']:
            new_values = []
            for value in self.nomination_state.get(key, []):
                # Make sure the value has transactions
                if hasattr(value, 'transactions'):
                    # Filter out any transactions whose hash is in finalized_hashes
                    filtered_tx = {tx for tx in value.transactions if tx.hash not in finalized_hashes}
                    if filtered_tx:
                        new_values.append(Value(transactions=filtered_tx))
                    else:
                        log.node.info("Removing an empty nomination value from '%s'.", key)
                else:
                    # If for some reason the value does not have transactions, keep it unchanged
                    new_values.append(value)
            self.nomination_state[key] = new_values

        self.nomination_round = 1


    def prune_nomination_phase_data(self):
        # Step 1: Collect finalized Value hashes from the ledger.
        finalized_value_hashes = set()
        for slot_data in self.ledger.slots.values():
            value = slot_data.get('value')
            if not value:
                continue
            finalized_value_hashes.add(value.hash)

        log.node.info("Pruning nomination data: finalized Value hashes: %s", finalized_value_hashes)

        # Step 2: Prune statement_counter.
        new_statement_counter = {vh: counters for vh, counters in self.statement_counter.items()
                                 if vh not in finalized_value_hashes}
        for vh in self.statement_counter:
            if vh in finalized_value_hashes:
                log.node.info("Pruning statement counter for finalized value %s", vh)
        self.statement_counter = new_statement_counter

        def flatten(values):
            flat = []
            for item in values:
                if isinstance(item, list):
                    flat.extend(item)
                else:
                    flat.append(item)
            return flat

        # Step 3: Prune broadcast_flags.
        new_broadcast_flags = []
        for msg in self.broadcast_flags:
            remove_flag = False
            candidate_values = None
            if hasattr(msg, "ballot"):
                candidate_values = getattr(msg.ballot, "value", None)

            if candidate_values is None and hasattr(msg, "_voted"):
                candidate_values = msg._voted
            if candidate_values:

                if isinstance(candidate_values, list):
                    candidates = flatten(candidate_values)
                else:
                    candidates = [candidate_values]

                for candidate in candidates:
                    if hasattr(candidate, "hash"):
                        if candidate.hash in finalized_value_hashes:
                            remove_flag = True
                            log.node.info("Pruning broadcast message %s because candidate Value %s is finalized",
                                          msg, candidate.hash)
                            break
            if not remove_flag:
                new_broadcast_flags.append(msg)
        self.broadcast_flags = new_broadcast_flags

        log.node.info("Pruning complete. Updated statement_counter: %s, broadcast_flags count: %d",
                      self.statement_counter, len(self.broadcast_flags))
