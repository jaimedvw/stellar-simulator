"""
=========================
Quorum Set
=========================

Author: Matija Piskorec, Jaime de Vivero Woods
Last update: June 2025
QuorumSet class.
"""
import math
import random
from Log import log

import numpy as np

THRESHOLD_DEFAULT = 55 # 25% threshold by default

class QuorumSet():

    def __init__(self, node, inner_sets=None, **kvargs):

        if 'threshold' in kvargs:
            assert type(kvargs['threshold']) in (float, int)
            assert kvargs['threshold'] <= 100 and kvargs['threshold'] > 0  # threshold must be percentile

        # Quorum set knows to which node it belongs to
        self.node = node
        self.threshold = kvargs['threshold'] if 'threshold' in kvargs else THRESHOLD_DEFAULT

        self.nodes = []
        #self.inner_sets = [] # will keep to 1 layer of depth for now, rarely gets deeper
        self.inner_sets = inner_sets if inner_sets is not None else []

        log.quorum.info('Initialized quorum set for Node %s, threshold=%s, nodes=%s, inner sets=%s.',
                        self.node, self.threshold, self.nodes, self.inner_sets)

    def __repr__(self):
        return '[Quorum Set: threshold=%(threshold)s, nodes=%(nodes)s.' % self.__dict__

    def is_inside(self, node):
        if node in self.nodes:
            return True
        # Recursively check inner quorum sets
        return any(inner_set.is_inside(node) for inner_set in self.inner_sets)


    # Remove node from quorum set
    def remove(self, node):
        self.nodes = filter(lambda x: x != node, self.nodes)
        return

    # Set quorum to the nodes
    def set(self, nodes, inner_sets):
        self.nodes = nodes if isinstance(nodes, list) else [nodes]
        self.inner_sets = inner_sets if inner_sets is not None else []

        #log.quorum.info('Set nodes %s as the quorum set of Node %s.', self.nodes, self.node)
        #log.quorum.info('Set inner sets %s as the inner quorum sets of Node %s.', self.inner_sets, self.node)
        log.quorum.info(
            "Set quorum for %s: threshold=%s, nodes=%s, inner_sets=%s",
            self.node, self.threshold, [n.name for n in self.nodes], self.inner_sets
        )


    def get_node(self):
        # If there are no nodes in the quorum set, return None
        if len(self.nodes) == 0:
            return None
        else:
            return np.random.choice([node for node in self.nodes if node != self.node])

    def get_nodes(self):
        return self.nodes.copy()

    def get_inner_sets(self):
        return self.inner_sets.copy()

    def _flatten(self, xs):
        out = []
        for x in xs:
            if isinstance(x, list):
                out.extend(self._flatten(x))
            else:
                out.append(x)
        return out

    @property
    def size(self):
        def _flatten(q):
            flat = []
            for x in q:
                if isinstance(x, list):
                    flat.extend(_flatten(x))
                else:
                    flat.append(x)
            return flat

        return len(self.nodes) + len(_flatten(self.inner_sets))

    @property
    def minimum_quorum(self):
        sz = self.size
        t = self.threshold

        if isinstance(t, float) and 0 < t <= 1:
            needed = math.ceil(sz * t)

        else:
            needed = math.ceil(sz * (t / 100.0))

        log.quorum.info("QuorumSet for %s: size=%d, raw threshold=%s → minimum_quorum=%d",self.node, sz, t, needed)
        return needed

    def get_quorum(self):
        return self.nodes.copy(), self._flatten(self.inner_sets)

    # This function checks if the quorum meets threshold - it checks every node, it doesn't check for nested QuorumSlices
    def check_threshold(self, val, quorum, threshold, node_statement_counter):
        entry = node_statement_counter.get(val.hash, {"voted": set(), "accepted": set()})
        signed = 0
        for peer in self._flatten(quorum):
            if peer.name in entry.get("voted", set()) or peer.name in entry.get("accepted", set()):
                signed += 1
        return signed >= threshold

    def check_prepare_threshold(self, ballot, quorum, threshold, prepare_statement_counter):
        if ballot.value not in prepare_statement_counter:
            return False

        entry = prepare_statement_counter[ballot.value]
        flat_peers = self._flatten(quorum)
        signed = 0
        seen = set()

        for peer in flat_peers:
            if peer in seen:
                continue

            for state in ('voted', 'accepted'):
                if peer in entry.get(state, set()):
                    seen.add(peer)
                    signed += 1
                    break

        return signed >= threshold

    def check_commit_threshold(self, ballot, quorum, threshold, commit_statement_counter):
        if ballot.value not in commit_statement_counter:
            return False

        entry = commit_statement_counter[ballot.value]
        flat_peers = self._flatten(quorum)
        signed = 0
        seen = set()

        for peer in flat_peers:
            if peer in seen:
                continue

            for state in ('voted', 'accepted'):
                if peer in entry.get(state, set()):
                    seen.add(peer)
                    signed += 1
                    break

        return signed >= threshold


    def check_inner_set_blocking_threshold(self, calling_node, val, quorum):
        flat_peers = self._flatten(quorum)

        count = 0
        voted = calling_node.statement_counter[val.hash]["voted"]
        accepted = calling_node.statement_counter[val.hash]["accepted"]

        seen = set()
        for peer in flat_peers:
            if peer is calling_node or peer in seen:
                continue
            if peer in voted or peer in accepted:
                seen.add(peer)
                count += 1

        return count

    def get_nodes_with_broadcast_prepare_msgs(self, calling_node, quorum):
        broadcast_nodes = []
        for node in quorum:
            if (node != calling_node) and len(node.ballot_prepare_broadcast_flags) >= 1:
                broadcast_nodes.append(node)

        return broadcast_nodes

    def retrieve_random_peer(self, calling_node):

        def _flatten_quorum_list(q):
            flattened = []
            for x in q:
                if isinstance(x, list):
                    flattened.extend(_flatten_quorum_list(x))
                else:
                    flattened.append(x)
            return flattened

        candidates = [n for n in self.nodes if n is not calling_node]

        for inner in self.inner_sets:
            for n in _flatten_quorum_list([inner]):
                if n is not calling_node:
                    candidates.append(n)

        return random.choice(candidates) if candidates else None

    def weight(self, v):
        count = self.nodes.count(v) # Count how many times 'v' appears in slices

        # Include inner quorum sets
        for inner_set in self.inner_sets:
            if isinstance(inner_set, list):
                if v in inner_set:
                    count += 1
            elif v == inner_set:
                count += 1

        # Compute fraction of quorum slices that contain 'v'
        total_slices = len(self.nodes) + len(self.inner_sets)

        if self == v:
            count = total_slices
            return count / total_slices

        if total_slices == 0:
            return 0.0  # Avoid division by zero

        return count / total_slices