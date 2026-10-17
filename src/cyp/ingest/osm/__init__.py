"""OpenStreetMap lookups for the RIDE.LOG route story (flag ``strava.route_story``, ADR-0021).

:mod:`~cyp.ingest.osm.privacy` trims and samples a ride's coordinates before anything leaves the
machine; :mod:`~cyp.ingest.osm.client` asks Overpass (places along the route) and Nominatim
(district names). Callers cache the results per ride.
"""
