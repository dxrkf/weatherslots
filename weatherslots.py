#!/usr/bin/env python3
"""Find hourly forecast windows matching explicit weather filters. Online CLI."""
import argparse
import html
import json
import math
import os
import tempfile
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
MAX_BYTES = 2000000
FIELDS = ('temperature_2m', 'wind_speed_10m', 'precipitation_probability')

@dataclass(frozen=True)
class Filters:
    min_temp: float = 10
    max_temp: float = 30
    max_wind: float = 20
    max_rain: float = 20
    duration: int = 2
    from_hour: int = 6
    to_hour: int = 22

def validate_filters(f):
    if not all((type(x) in (int, float) and math.isfinite(x) for x in (f.min_temp, f.max_temp, f.max_wind, f.max_rain))):
        raise ValueError('filters must be finite numbers')
    if f.min_temp > f.max_temp or f.max_wind < 0 or (not 0 <= f.max_rain <= 100):
        raise ValueError('invalid weather filter range')
    if type(f.duration) is not int or not 1 <= f.duration <= 12:
        raise ValueError('duration must be 1 to 12 elapsed hours')
    if type(f.from_hour) is not int or type(f.to_hour) is not int or (not 0 <= f.from_hour < f.to_hour <= 24):
        raise ValueError('availability must be a same-day interval within 0..24')

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, *args):
        return None

def fetch(latitude, longitude, days=3, timeout=30, opener=None):
    if not all((type(x) in (int, float) and math.isfinite(x) for x in (latitude, longitude))) or not -90 <= latitude <= 90 or (not -180 <= longitude <= 180):
        raise ValueError('invalid coordinates')
    if type(days) is not int or not 1 <= days <= 7:
        raise ValueError('forecast days must be 1 to 7')
    query = urllib.parse.urlencode(dict(latitude=latitude, longitude=longitude, hourly=','.join(FIELDS), forecast_days=days, timezone='UTC', timeformat='unixtime', temperature_unit='celsius', wind_speed_unit='kmh'))
    req = urllib.request.Request('https://api.open-meteo.com/v1/forecast?' + query, headers={'User-Agent': 'WeatherSlots/1.0', 'Accept': 'application/json'})
    open_request = opener or urllib.request.build_opener(NoRedirect()).open
    try:
        with open_request(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('forecast exceeds 2 MB limit')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'weather API returned HTTP {exc.code}; check access, parameters or rate limits') from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ValueError('weather API network/TLS request failed or timed out') from None

def samples(data):
    if not isinstance(data, dict) or not isinstance(data.get('hourly'), dict) or (not isinstance(data.get('hourly_units'), dict)):
        raise ValueError('invalid forecast response')
    hourly = data['hourly']
    units = data['hourly_units']
    if units.get('time') != 'unixtime' or units.get(FIELDS[0]) != '°C' or units.get(FIELDS[1]) != 'km/h' or (units.get(FIELDS[2]) != '%'):
        raise ValueError('unexpected forecast units')
    times = hourly.get('time')
    if not isinstance(times, list) or not 2 <= len(times) <= 500 or any((type(t) is not int for t in times)) or any((b <= a for a, b in zip(times, times[1:]))):
        raise ValueError('invalid or unordered forecast timestamps')
    arrays = [hourly.get(k) for k in FIELDS]
    if any((not isinstance(a, list) or len(a) != len(times) for a in arrays)):
        raise ValueError('forecast array lengths differ')
    rows = []
    for i, t in enumerate(times):
        values = [a[i] for a in arrays]
        if any((x is not None and (type(x) not in (int, float) or not math.isfinite(x)) for x in values)):
            raise ValueError('invalid forecast value')
        temp, wind, rain = values
        if wind is not None and wind < 0 or (rain is not None and (not 0 <= rain <= 100)):
            raise ValueError('invalid wind/rain value')
        datetime.fromtimestamp(t, timezone.utc)
        rows.append(dict(time=t, temp=temp, wind=wind, rain=rain))
    return rows

def available(start, end, f):
    end_hour = end.hour + end.minute / 60 + end.second / 3600
    if start.date() != end.date():
        if not (f.to_hour == 24 and end.date() == start.date() + timedelta(days=1) and (end_hour == 0)):
            return False
        end_hour = 24
    start_hour = start.hour + start.minute / 60 + start.second / 3600
    return start_hour >= f.from_hour and end_hour <= f.to_hour

def select(rows, f, zone, now):
    validate_filters(f)
    windows = []
    n = f.duration
    for i in range(len(rows) - n):
        block = rows[i:i + n + 1]
        if block[0]['time'] < now or any((b['time'] - a['time'] != 3600 for a, b in zip(block, block[1:]))):
            continue
        start = datetime.fromtimestamp(block[0]['time'], zone)
        end = datetime.fromtimestamp(block[-1]['time'], zone)
        if not available(start, end, f):
            continue
        # Temperature/wind are boundary samples; rain describes the preceding hour.
        # Endpoint rain samples 1..n cover the candidate interval.
        temps = [r['temp'] for r in block]
        winds = [r['wind'] for r in block]
        rains = [r['rain'] for r in block[1:]]
        if any((x is None for x in temps + winds + rains)):
            continue
        if min(temps) < f.min_temp or max(temps) > f.max_temp or max(winds) > f.max_wind or (max(rains) > f.max_rain):
            continue
        windows.append(dict(start=start.isoformat(), end=end.isoformat(), duration_hours=n, min_temp_c=min(temps), max_temp_c=max(temps), max_wind_kmh=max(winds), max_hourly_rain_probability=max(rains)))
    return windows

def build_report(data, f, zone_name, now, latitude, longitude):
    zone = ZoneInfo(zone_name)
    rows = samples(data)
    windows = select(rows, f, zone, now)
    return dict(format='weatherslots-report-v1', fetched_at_utc=datetime.fromtimestamp(now, timezone.utc).isoformat(), timezone=zone_name, requested_coordinates=dict(latitude=latitude, longitude=longitude), source='Open-Meteo Weather Forecast API', source_url='https://open-meteo.com/', attribution='Weather data by Open-Meteo, CC BY 4.0', filters=asdict(f), sample_count=len(rows), samples_with_missing_values=sum((any((r[k] is None for k in ('temp', 'wind', 'rain'))) for r in rows)), matching_windows=len(windows), windows=windows, interpretation='Hourly forecast samples match your filters; this is not a weather safety guarantee or an official warning service.')

def render_html(report):
    rows = []
    for w in report['windows']:
        cells = [w['start'], w['end'], f"{w['min_temp_c']:g}–{w['max_temp_c']:g} °C", f"{w['max_wind_kmh']:g} km/h", f"{w['max_hourly_rain_probability']:g}%"]
        rows.append('<tr>' + ''.join(('<td>' + html.escape(str(v)) + '</td>' for v in cells)) + '</tr>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>WeatherSlots</title><style>body{font:17px system-ui;background:#101827;color:#e2e8f0;max-width:1200px;margin:45px auto;padding:24px}h1,a{color:#5eead4}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #334155}.scroll{overflow-x:auto}p{line-height:1.6}</style><h1>WeatherSlots / matching forecast windows</h1><p>Fetched: ' + html.escape(report['fetched_at_utc']) + ' · Zone: ' + html.escape(report['timezone']) + '</p><p>' + html.escape(report['interpretation']) + '</p><p>Matches: ' + str(report['matching_windows']) + '</p><div class="scroll"><table><tr><th>Start</th><th>End</th><th>Boundary temperatures</th><th>Max boundary wind</th><th>Max hourly rain probability</th></tr>' + ''.join(rows) + '</table></div><p>Filters: ' + html.escape(json.dumps(report['filters'])) + '</p><p>Weather data by <a href="https://open-meteo.com/">Open-Meteo</a>, <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. Re-run the CLI to refresh.</p></html>'

def atomic_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('output symlinks refused')
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--latitude', type=float, required=True)
    p.add_argument('--longitude', type=float, required=True)
    p.add_argument('--timezone', required=True)
    p.add_argument('--days', type=int, default=3)
    p.add_argument('--duration', type=int, default=2)
    p.add_argument('--min-temp', type=float, default=10)
    p.add_argument('--max-temp', type=float, default=30)
    p.add_argument('--max-wind', type=float, default=20)
    p.add_argument('--max-rain', type=float, default=20)
    p.add_argument('--from-hour', type=int, default=6)
    p.add_argument('--to-hour', type=int, default=22)
    p.add_argument('--timeout', type=int, default=30)
    p.add_argument('--html')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    try:
        if not 1 <= a.timeout <= 60:
            raise ValueError('timeout must be 1 to 60 seconds')
        f = Filters(a.min_temp, a.max_temp, a.max_wind, a.max_rain, a.duration, a.from_hour, a.to_hour)
        validate_filters(f)
        ZoneInfo(a.timezone)
        data = fetch(a.latitude, a.longitude, a.days, a.timeout)
        report = build_report(data, f, a.timezone, datetime.now(timezone.utc).timestamp(), a.latitude, a.longitude)
        if a.html:
            atomic_write(a.html, render_html(report))
        if a.json:
            print(json.dumps(report, indent=2, ensure_ascii=True))
        else:
            print(f"WeatherSlots: {report['sample_count']} hourly samples, {report['matching_windows']} matching windows")
            for w in report['windows']:
                print(w['start'] + ' -> ' + w['end'] + f" | {w['min_temp_c']:g}..{w['max_temp_c']:g} C | wind <= {w['max_wind_kmh']:g} km/h | rain <= {w['max_hourly_rain_probability']:g}%")
            print('Forecast filters, not a safety guarantee. Data: Open-Meteo (CC BY 4.0).')
        return 0
    except (OSError, ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        print('error: invalid settings, unavailable timezone data, failed API request or invalid forecast; no fresh report produced', file=sys.stderr)
        return 2
if __name__ == '__main__':
    sys.exit(main())
