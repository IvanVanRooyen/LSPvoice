"""Validated, hospital-scoped inventory. No account, patient or payment authority."""
import json
from pathlib import Path
import re
from threading import Lock


def identifier(value):
    return ''.join(str(value or '').split()).casefold()


def hospital_alias(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


class InventoryError(ValueError):
    pass


class StaticInventory:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = Lock()
        self.stamp = None
        self.sites = {}
        self.aliases = {}
        self.version = None

    def _load(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # Before first activation, legacy synthetic fixtures still work.
            if self.stamp is not None:
                raise InventoryError('Activated inventory file is unavailable')
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self.stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema_version') != 1 or data.get('status') != 'active_lookup_only':
                raise InventoryError('Inventory has not been approved for activation')
            sites, aliases = {}, {}
            for site in data['sites']:
                code = site['site_code']
                if not re.fullmatch(r'[A-Z][A-Z0-9]{1,9}', code) or code in sites:
                    raise InventoryError('Invalid/duplicate inventory site')
                wards = {str(w['id']): w for w in site['wards']}
                if len(wards) != len(site['wards']):
                    raise InventoryError('Duplicate ward ID within site')
                beds, devices = {}, {}
                for row in site['devices']:
                    # An allowlist also protects against accidentally deploying a raw export.
                    if set(row) - {'site_code', 'id', 'did', 'deviceid', 'hospital',
                                   'name', 'location', 'screenlabel', 'device_type'}:
                        raise InventoryError('Unapproved inventory field')
                    ward = wards.get(str(row['name']))
                    if (row.get('site_code') != code or ward is None
                            or str(ward['hospital']) != str(row['hospital'])):
                        raise InventoryError('Invalid site/ward association')
                    bed, device = identifier(row['location']), identifier(row['deviceid'])
                    if not bed or not device or bed in beds or device in devices:
                        raise InventoryError('Blank or ambiguous bed/device in site')
                    safe = {'hospital_code': code, 'hospital_name': site['site_name'],
                            'bed_id': str(row['location']), 'device_id': str(row['deviceid']),
                            'ward': ward['wardname'], 'ward_id': code + ':' + str(ward['id']),
                            'device_type': row.get('device_type') or None}
                    beds[bed] = safe
                    devices[device] = safe
                sites[code] = {'beds': beds, 'devices': devices, 'wards': len(wards)}
                for alias in [code, site['site_name'], *site.get('aliases', [])]:
                    key = hospital_alias(alias)
                    if not key or (key in aliases and aliases[key] != code):
                        raise InventoryError('Ambiguous hospital alias')
                    aliases[key] = code
            if not sites:
                raise InventoryError('Inventory contains no sites')
            self.sites, self.aliases = sites, aliases
            self.version = data['archive_sha256']
            self.stamp = stamp
        except (KeyError, TypeError, ValueError, OSError) as error:
            if isinstance(error, InventoryError):
                raise
            raise InventoryError('Inventory validation failed') from None

    def lookup(self, hospital, bed_id='', device_id='', body_hospital=None):
        with self.lock:
            self._load()
            code = self.aliases.get(hospital_alias(hospital))
            if code is None:
                return None  # Main handles only the separately named synthetic hospitals.
            response = {'device_found': False, 'lookup_source': 'static_inventory',
                        'inventory_version': self.version, 'hospital_code': code,
                        'device_id': None, 'bed_id': None, 'ward': None, 'ward_id': None,
                        'device_type': None, 'account_state_known': False,
                        'patient_stay_known': False, 'package_active': None, 'ends_at': None,
                        'payment_pending_minutes': None, 'advance_used': None,
                        'balance_owing': None}
            if body_hospital and self.aliases.get(hospital_alias(body_hospital)) != code:
                return dict(response, lookup_reason='hospital_mismatch')
            site = self.sites[code]
            bed = site['beds'].get(identifier(bed_id)) if identifier(bed_id) else None
            device = site['devices'].get(identifier(device_id)) if identifier(device_id) else None
            if ((identifier(bed_id) and bed is None) or (identifier(device_id) and device is None)
                    or (bed is not None and device is not None and bed != device)):
                return dict(response, lookup_reason='not_found_or_identifiers_conflict')
            match = bed or device
            if match is None:
                return dict(response, lookup_reason='identifier_required')
            return dict(response, **match, device_found=True, lookup_reason='matched')

    def summary(self):
        with self.lock:
            self._load()
            return {'active': bool(self.sites), 'version': self.version,
                    'sites': {code: {'devices': len(site['devices']), 'wards': site['wards']}
                              for code, site in self.sites.items()}}
