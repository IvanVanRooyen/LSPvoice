"""Hospital isolation and unknown-account guarantees for exported inventory."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
from static_inventory import StaticInventory, InventoryError


class StaticInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inventory.json'
        self.data = {'schema_version': 1, 'status': 'active_lookup_only',
                     'archive_sha256': 'synthetic-version', 'sites': []}
        for code, name, device in [('RAH', 'Royal Adelaide Hospital', 'rah-device'),
                                   ('RNS', 'Royal North Shore Hospital', 'rns-device')]:
            self.data['sites'].append({'site_code': code, 'site_name': name,
                'wards': [{'id': 1, 'hospital': 1, 'wardname': code + ' ward'}],
                'devices': [{'site_code': code, 'id': 1, 'did': '1', 'deviceid': device,
                    'hospital': 1, 'name': 1, 'location': '101', 'device_type': 'UNMAPPED'}]})
        self.write()
        self.store = StaticInventory(self.path)

    def write(self):
        self.path.write_text(json.dumps(self.data), encoding='utf-8')

    def test_same_source_ids_and_bed_names_remain_isolated(self):
        self.assertEqual(self.store.lookup('Royal Adelaide', '101'), None)
        a = self.store.lookup('Royal Adelaide Hospital', '101')
        b = self.store.lookup('RNS', '101')
        self.assertEqual(a['device_id'], 'rah-device')
        self.assertEqual(b['device_id'], 'rns-device')
        self.assertEqual(a['ward_id'], 'RAH:1')
        self.assertEqual(b['ward_id'], 'RNS:1')

    def test_unknown_wrong_hospital_and_conflicting_identifiers(self):
        for args in [('RAH', 'missing', ''), ('RAH', '', 'rns-device'),
                     ('RAH', 'missing', 'rah-device'), ('RAH', '', '')]:
            self.assertFalse(self.store.lookup(*args)['device_found'])
        self.assertFalse(self.store.lookup('RAH', '101', body_hospital='RNS')['device_found'])
        self.assertIsNone(self.store.lookup('1', '101'))

    def test_case_and_spacing_and_device_only(self):
        self.assertTrue(self.store.lookup(' r a h ', '1 0 1')['device_found'])
        self.assertEqual(self.store.lookup('RAH', device_id='RAH-DEVICE')['bed_id'], '101')
        self.assertFalse(self.store.lookup('RAH', device_id='1')['device_found'])

    def test_static_identity_is_not_financial_or_stay_authority(self):
        found = self.store.lookup('RAH', '101')
        for name in ['package_active', 'ends_at', 'balance_owing', 'advance_used', 'payment_pending_minutes']:
            self.assertIsNone(found[name])
        self.assertFalse(found['account_state_known'])
        self.assertFalse(found['patient_stay_known'])
        self.assertEqual(found['lookup_source'], 'static_inventory')

    def test_duplicate_bed_and_secret_fields_fail_closed(self):
        for change in ['duplicate', 'secret']:
            data = copy.deepcopy(self.data)
            if change == 'duplicate':
                data['sites'][0]['devices'].append(dict(data['sites'][0]['devices'][0], deviceid='other'))
            else:
                data['sites'][0]['devices'][0]['extrabillapikey'] = 'synthetic-do-not-expose'
            self.path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(InventoryError):
                StaticInventory(self.path).lookup('RAH', '101')

    def test_missing_after_activation_and_invalid_replacement_fail_closed(self):
        self.store.lookup('RAH', '101')
        self.path.unlink()
        with self.assertRaises(InventoryError):
            self.store.lookup('RAH', '101')
        self.path.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(InventoryError):
            self.store.lookup('RAH', '101')

    def test_authenticated_api_and_no_synthetic_fallback_for_real_site(self):
        with patch.object(main, 'INVENTORY', self.store), patch.object(main, 'BEARER', 'synthetic-auth'):
            client = TestClient(main.app)
            path = '/hospitals/RAH/devices/lookup'
            self.assertEqual(client.post(path, json={'bed_id': '101'}).status_code, 401)
            headers = {'Authorization': 'Bearer synthetic-auth'}
            response = client.post(path, json={'bed_id': '101'}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['device_id'], 'rah-device')
            encoded = client.post('/hospitals/Royal%2520Adelaide%2520Hospital/devices/lookup',
                                  json={'bed_id': '101'}, headers=headers)
            self.assertEqual(encoded.json()['device_id'], 'rah-device')
            self.assertFalse(client.post(path, json={'device_id': 'test-ok'}, headers=headers).json()['device_found'])
            demo = client.post('/hospitals/GH/devices/lookup', json={'device_id': 'test-ok'}, headers=headers).json()
            self.assertTrue(demo['device_found'])
            self.assertEqual(demo['lookup_source'], 'synthetic_fixture')
            self.assertFalse(client.post('/mock/stays/resolve', json={'hospital': 'RAH', 'bed_id': '101'}, headers=headers).json()['stay_resolved'])

    def test_invalid_inventory_api_is_unavailable_instead_of_fake_match(self):
        self.path.write_text('{invalid', encoding='utf-8')
        with patch.object(main, 'INVENTORY', self.store), patch.object(main, 'BEARER', 'synthetic-auth'):
            response = TestClient(main.app).post('/hospitals/RAH/devices/lookup',
                json={'bed_id': '101'}, headers={'Authorization': 'Bearer synthetic-auth'})
            self.assertEqual(response.status_code, 503)


if __name__ == '__main__':
    unittest.main()
