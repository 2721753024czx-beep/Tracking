import unittest

from tracking3.timing import coordinate, latest, metadata


def media(ident, group='recording', error=1, relative=0):
    return dict(id=ident, reference_group=group, relative_start=relative,
                start_epoch=None, uncertainty=error, offset=0)


class TimingTests(unittest.TestCase):
    def test_relative_clock_does_not_invent_date(self):
        m = media('a', relative=12)
        m['offset'] = -2
        self.assertEqual(coordinate(m, 4), (('relative', 'recording'), 14))
        self.assertIsNone(m['start_epoch'])

    def test_nonzero_error_can_prove_last_when_ranges_separate(self):
        events = [(media('a'), {'id':'a', 'end':10}), (media('b'), {'id':'b', 'end':15})]
        self.assertEqual(latest(events), {'status':'unique', 'observation_id':'b'})

    def test_overlap_tie_and_unknown_error_do_not_choose_unique_last(self):
        for second in [10, 11, 12]:
            events = [(media('a'), {'id':'a', 'end':10}), (media('b'), {'id':'b', 'end':second})]
            self.assertEqual(latest(events)['status'], 'overlap_or_tie')
        events[1][0]['uncertainty'] = None
        self.assertEqual(latest(events)['status'], 'unknown_error')

    def test_different_groups_and_file_only_are_not_global_chronology(self):
        events = [(media('a'), {'id':'a', 'end':10}), (media('b', group='another'), {'id':'b', 'end':150})]
        self.assertEqual(latest(events)['status'], 'incomparable')
        m = media('a', group=None, relative=None)
        self.assertEqual(latest([(m, {'id':'a', 'end':10})])['status'], 'local_only')

    def test_group_without_evidence_or_finite_origin_is_rejected(self):
        for data in [dict(reference_group='a'), dict(reference_group='a',relative_start=0),
                     dict(reference_group='a',relative_start=float('nan'),time_evidence='note'),
                     dict(uncertainty=-1)]:
            with self.assertRaises(ValueError):
                metadata(data)


if __name__ == '__main__':
    unittest.main()
