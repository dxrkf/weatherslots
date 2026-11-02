import copy
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import weatherslots as w
BASE = int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())

def fixture():
    return {'hourly_units': {'time': 'unixtime', w.FIELDS[0]: '°C', w.FIELDS[1]: 'km/h', w.FIELDS[2]: '%'}, 'hourly': {'time': [BASE + i * 3600 for i in range(5)], w.FIELDS[0]: [22] * 5, w.FIELDS[1]: [10] * 5, w.FIELDS[2]: [0] * 5}}

def windows(data=None, **kw):
    return w.select(w.samples(data or fixture()), w.Filters(from_hour=0, to_hour=24, **kw), ZoneInfo('UTC'), BASE)

class WeatherTests(unittest.TestCase):

    def test_two_hour_windows_need_three_boundaries(self):
        self.assertEqual(len(windows()), 3)

    def test_thresholds_are_inclusive(self):
        self.assertEqual(len(windows(min_temp=22, max_temp=22, max_wind=10, max_rain=0)), 3)

    def test_endpoint_heat_excludes_window(self):
        d = fixture()
        d['hourly'][w.FIELDS[0]][2] = 31
        self.assertEqual(windows(d), [])

    def test_preceding_rain_alignment(self):
        d = fixture()
        d['hourly'][w.FIELDS[2]][0] = 100
        self.assertEqual(len(windows(d)), 3)
        d['hourly'][w.FIELDS[2]][2] = 100
        self.assertEqual(len(windows(d)), 1)
        self.assertTrue(windows(d)[0]['start'].startswith('2026-11-02T02:00'))

    def test_missing_values_exclude_affected_windows(self):
        d = fixture()
        d['hourly'][w.FIELDS[1]][0] = None
        self.assertEqual(len(windows(d)), 2)

    def test_gap_is_not_a_continuous_window(self):
        d = fixture()
        d['hourly']['time'][2:] = [BASE + i * 3600 for i in (3, 4, 5)]
        self.assertEqual(len(windows(d)), 1)

    def test_past_starts_excluded(self):
        self.assertEqual(len(w.select(w.samples(fixture()), w.Filters(from_hour=0, to_hour=24), ZoneInfo('UTC'), BASE + 1)), 2)

    def test_local_availability(self):
        self.assertEqual(len(w.select(w.samples(fixture()), w.Filters(from_hour=4, to_hour=6), ZoneInfo('Asia/Riyadh'), BASE)), 1)

    def test_fractional_timezone(self):
        self.assertEqual(len(w.select(w.samples(fixture()), w.Filters(from_hour=6, to_hour=8), ZoneInfo('Asia/Kolkata'), BASE)), 0)

    def test_midnight_end_allowed(self):
        start = datetime(2026, 11, 2, 22, tzinfo=timezone.utc)
        end = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.assertTrue(w.available(start, end, w.Filters(to_hour=24)))
        self.assertFalse(w.available(start, end, w.Filters(to_hour=23)))

    def test_dst_duration_is_elapsed_time(self):
        d = fixture()
        t = int(datetime(2026, 11, 1, 5, tzinfo=timezone.utc).timestamp())
        d['hourly']['time'] = [t + i * 3600 for i in range(5)]
        a = w.select(w.samples(d), w.Filters(from_hour=0, to_hour=24), ZoneInfo('America/New_York'), t)[0]
        self.assertEqual((datetime.fromisoformat(a['end']) - datetime.fromisoformat(a['start'])).total_seconds(), 7200)

    def test_bad_filters(self):
        for f in [w.Filters(duration=0), w.Filters(max_rain=101), w.Filters(min_temp=float('nan')), w.Filters(from_hour=20, to_hour=6), w.Filters(max_wind=-1)]:
            with self.subTest(f=f), self.assertRaises(ValueError):
                w.validate_filters(f)

    def test_bad_units(self):
        d = fixture()
        d['hourly_units'][w.FIELDS[1]] = 'mph'
        with self.assertRaises(ValueError):
            w.samples(d)

    def test_bad_arrays(self):
        d = fixture()
        d['hourly'][w.FIELDS[0]].pop()
        with self.assertRaises(ValueError):
            w.samples(d)

    def test_bad_timestamps(self):
        for value in [BASE, True]:
            d = fixture()
            d['hourly']['time'][1] = value
            with self.assertRaises(ValueError):
                w.samples(d)

    def test_bad_numeric_data(self):
        for val in [True, float('inf'), '22']:
            d = fixture()
            d['hourly'][w.FIELDS[0]][0] = val
            with self.assertRaises(ValueError):
                w.samples(d)

    def test_fetch_request_and_decoding(self):

        def fake(req, timeout):
            self.assertTrue(req.full_url.startswith('https://api.open-meteo.com/v1/forecast?'))
            self.assertIn('wind_speed_unit=kmh', req.full_url)
            self.assertEqual(timeout, 30)
            return io.BytesIO(json.dumps(fixture()).encode())
        self.assertEqual(w.fetch(24.7136, 46.6753, opener=fake), fixture())

    def test_network_and_rate_limit(self):
        for error in [urllib.error.URLError('offline'), urllib.error.HTTPError('url', 429, 'limit', {}, None)]:

            def fake(*args, **kw):
                raise error
            with self.assertRaises(ValueError):
                w.fetch(0, 0, opener=fake)

    def test_size_limit(self):
        with self.assertRaises(ValueError):
            w.fetch(0, 0, opener=lambda *a, **k: io.BytesIO(b'x' * (w.MAX_BYTES + 1)))

    def test_coordinates_validation_before_network(self):
        for a, b in [(91, 0), (0, 181), (float('nan'), 0)]:
            with self.assertRaises(ValueError):
                w.fetch(a, b, opener=lambda *a, **k: self.fail('network should not run'))

    def test_report_and_html_escape(self):
        r = w.build_report(fixture(), w.Filters(from_hour=0, to_hour=24), 'UTC', BASE, 0, 0)
        self.assertEqual(r['matching_windows'], 3)
        r['timezone'] = '<script>alert(1)</script>'
        doc = w.render_html(r)
        self.assertNotIn('<script>', doc)
        self.assertIn('&lt;script&gt;', doc)
        self.assertIn('CC BY 4.0', doc)

    def test_atomic_output_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'report.html'
            w.atomic_write(p, 'old')
            w.atomic_write(p, 'new')
            self.assertEqual(p.read_text(), 'new')
            link = Path(d) / 'link.html'
            link.symlink_to(p)
            with self.assertRaises(ValueError):
                w.atomic_write(link, 'bad')
            self.assertEqual(p.read_text(), 'new')
if __name__ == '__main__':
    unittest.main()
