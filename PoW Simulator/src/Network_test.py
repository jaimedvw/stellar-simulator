from Log import log
import unittest
from Network import Network

class NetworkTest(unittest.TestCase):
    def setup(self):
        pass

    def test_generate_nodes_ER(self):
        topology = 'ER-SINGLEQUORUMSET'
        nodes = Network.generate_nodes(n_nodes=5, topology=topology)

        for node in nodes:
                log.test.debug('Node %s, all peers in quorum set = %s',node.name,node.quorum_set.get_nodes())
                self.assertTrue(len(node.quorum_set.nodes) >= 1)

                # If 3 nodes are filtered to be added to the current node, 1 should be added to the Quorum and the 2 others should be added as distinct sets
                if len(node.quorum_set.nodes) > 2:
                    self.assertTrue(len(node.quorum_set.inner_sets) > 1)

    def test_generate_nodes_FULL(self):
        topology = 'FULL'
        nodes = Network.generate_nodes(n_nodes=5, topology=topology)

        for node in nodes:
                log.test.debug('Node %s, all peers in quorum set = %s',node.name,node.quorum_set.get_nodes())
                self.assertTrue(len(node.quorum_set.nodes) > 1)

if __name__ == "__main__":
    unittest.main()