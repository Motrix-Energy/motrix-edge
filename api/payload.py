"""Shared rules for walking a device payload.

Device payloads have no common shape and no serialisation contract — P1 is a nested OBIS
tree, a Shelly plug is flat, a future device could hold bytes or a datetime. Three
surfaces walk them for different purposes and produce genuinely different output:

- `storage/csv_file.py` sanitises in place, so the JSON cell stays parseable;
- `storage/influxdb.py` flattens to dotted field names;
- `services/rest_api.py` encodes to JSON-safe types for the response.

Merging the traversals would be the wrong altitude. What must not diverge is the *policy*
they share, which is what lives here. Both values below used to be spelled out three
times, and had already drifted: `rest_api` bounded on `>` where the other two bounded on
`>=`, while a comment in `csv_file.py` asserted all three agreed.
"""
from math import isfinite
from typing import Union

# Past this depth a walker stops descending. Deliberately *not* a truncation rule for the
# storage walkers: `csv_file` hands the value back untouched and lets json.dumps decide, so
# a pathological payload — a circular reference, or a non-finite nested deeper than this —
# fails exactly the way it fails today, caught by StorageManager, instead of blowing the
# stack. `rest_api` degrades to str() instead, because a raise there is a 500 for the whole
# device list.
MAX_DEPTH = 10


def finite_or_str(value: float) -> Union[float, str]:
	"""Pass a float through, or replace a non-finite one with its str() form.

	`json.dumps` defaults to `allow_nan=True` and emits bare `NaN`/`Infinity`/`-Infinity` —
	valid Python, invalid JSON. A consumer then loses the whole row's payload rather than
	the one offending field, because the cell no longer parses at all; in a browser it is
	`JSON.parse` throwing on the entire response. "nan"/"inf"/"-inf" is the one encoding
	both the CSV and the REST surface emit, so a consumer meets one convention, not two.
	"""
	return value if isfinite(value) else str(value)
