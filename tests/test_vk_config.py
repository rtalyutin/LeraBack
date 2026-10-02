"""Protect community routing when queues/dialogs are owned by salon."""
import json
import unittest

from vk_config import integrations


class CommunityRouting(unittest.TestCase):
    def binding(self, salon, group):
        return {"salon_id": salon, "group_id": group, "secret": "synthetic", "confirmation_code": "synthetic"}

    def test_independent_communities_require_independent_queue_owners(self):
        self.assertEqual(len(integrations({"VK_INTEGRATIONS_JSON": json.dumps([
            self.binding(1, 101), self.binding(2, 102)])})), 2)
        for bindings in ([self.binding(1, 101), self.binding(1, 102)],
                         [self.binding(1, 101), self.binding(2, 101)]):
            with self.assertRaises(ValueError):
                integrations({"VK_INTEGRATIONS_JSON": json.dumps(bindings)})

    def test_absent_and_mixed_credentials_never_create_ambiguous_workers(self):
        self.assertEqual(integrations({}), [])
        with self.assertRaises(ValueError):
            integrations({"VK_INTEGRATIONS_JSON": "[]", "VK_COMMUNITY_TOKEN": "synthetic"})


if __name__ == "__main__":
    unittest.main()
