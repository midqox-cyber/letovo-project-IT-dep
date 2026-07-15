import importlib.util
import os
from pathlib import Path
import secrets
import tempfile


ROOT = Path(__file__).resolve().parents[1]


with tempfile.TemporaryDirectory() as data_dir:
    os.environ.update({
        "GREENNET_DATA": data_dir,
        "GREENNET_ADMIN_PASSWORD": secrets.token_urlsafe(18),
        "GREENNET_DEMO": "0",
    })
    spec = importlib.util.spec_from_file_location("greennet_parser_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    parser = module._NimphCardsParser()
    parser.feed("""
      <article class="nimph-sensor" data-sensor-id="W001" data-sensor-type="water"
               data-latitude="-15.7018" data-longitude="-50.5789"
               data-last-timestamp="2026-07-15 00:30:00" data-packet-id="81508"
               data-battery="88.9">
        <table>
          <tr class="nimph-reading" data-parameter="pH" data-value="7.12"
              data-unit="pH" data-quality="OK" data-timestamp="2026-07-15 00:30:00"
              data-primary="true"></tr>
        </table>
      </article>
      <article class="nimph-sensor" data-sensor-id="invalid id" data-sensor-type="water"
               data-latitude="0" data-longitude="0"></article>
    """)

    assert len(parser.sensors) == 1
    sensor = parser.sensors[0]
    assert sensor["sensor_id"] == "W001"
    assert sensor["sensor_type"] == "water"
    assert sensor["latitude"] == -15.7018
    assert sensor["longitude"] == -50.5789
    assert sensor["parameters"]["pH"] == {
        "value": 7.12,
        "unit": "pH",
        "quality_flag": "OK",
        "timestamp": "2026-07-15 00:30:00",
        "primary": True,
    }

print("NIMPH parser test passed")
