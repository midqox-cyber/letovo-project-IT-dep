# NIMPH integration

The upstream point API returns one sensor, one parameter and one moment per request. GreenNet Crisis therefore uses two complementary paths:

- the station registry for sensor identifiers, types and WGS 84 coordinates;
- the `/monitor` HTML snapshot for the complete current field view exposed through documented `data-*` attributes.

## Collection sequence

1. Request the monitoring HTML with a bounded response size and timeout.
2. Parse only `<article class="nimph-sensor">` and `<tr class="nimph-reading">` attributes.
3. Validate sensor identifiers, numeric coordinates, parameter names and field lengths.
4. Merge readings with the station registry so all known stations remain visible.
5. Cache the result and persist compressed snapshots in SQLite.
6. If the upstream source is incomplete, return a marked partial view and retain the last useful local state.

## Map behavior

- All stations are positioned by latitude and longitude, not by arbitrary screen coordinates.
- Water, soil and air stations have distinct colors and filters.
- A station card explains its location, last transmission, battery, packet quality and available readings.
- The frontend must present partial or stale states honestly instead of implying that every value is live.

CI tests the HTML parser with a synthetic fixture and does not depend on the availability of the external service.
