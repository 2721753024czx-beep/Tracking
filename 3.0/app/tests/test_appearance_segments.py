import unittest
import numpy as np

from tracking3.appearance_segments import propose


class AppearanceSegments(unittest.TestCase):
    def test_sustained_coherent_change_splits_at_first_new_sample(self):
        result=propose([[1,0],[1,0],[0,1],[0,1],[0,1]],threshold=.7)
        self.assertEqual(result['segments'],[[0,1],[2,3,4]])
        self.assertEqual(result['cuts'][0]['first_new_index'],2)
        self.assertEqual(result['cuts'][0]['confirmed_at_index'],3)
        self.assertFalse(result['identity_certified'])

    def test_isolated_noise_and_unconfirmed_tail_do_not_split_or_pollute_reference(self):
        result=propose([[1,0],[0,1],[1,0],[1,0],[0,1]],threshold=.7)
        self.assertEqual(result['segments'],[[0,1,2,3,4]])
        self.assertEqual(result['cuts'],[])

    def test_two_mutually_incoherent_points_do_not_confirm_each_other(self):
        result=propose([[1,0,0],[0,1,0],[0,0,1],[0,0,1]],threshold=.7)
        self.assertEqual(result['segments'],[[0,1],[2,3]])

    def test_repeated_changes_preserve_every_sample_exactly_once_in_order(self):
        vectors=[[1,0]]*4+[[0,1]]*3+[[1,0]]*2
        result=propose(vectors,threshold=.7)
        self.assertEqual(result['segments'],[list(range(4)),list(range(4,7)),[7,8]])
        self.assertEqual(sum(result['segments'],[]),list(range(9)))

    def test_magnitude_and_identity_metadata_are_not_inputs(self):
        a=propose([[1,0],[1,0],[0,1],[0,1]],threshold=.7)
        b=propose([[100,0],[.01,0],[0,9],[0,10]],threshold=.7)
        self.assertEqual(a['segments'],b['segments'])

    def test_empty_single_stable_and_invalid_arrays(self):
        self.assertEqual(propose([],threshold=.7)['segments'],[])
        self.assertEqual(propose([[1,0]],threshold=.7)['segments'],[[0]])
        self.assertEqual(propose([[1,0]]*8,threshold=.7)['segments'],[list(range(8))])
        for vectors in ([[0,0]],[[np.nan,1]],[[1,0],[1]],[[np.inf,1]]):
            with self.subTest(vectors=vectors),self.assertRaises(ValueError):propose(vectors,threshold=.7)
        for threshold in (0,1,float('nan'),True):
            with self.assertRaises(ValueError):propose([[1,0]],threshold=threshold)
        with self.assertRaises(ValueError):propose([[1,0]],threshold=.7,window=0)


if __name__=='__main__':unittest.main()
