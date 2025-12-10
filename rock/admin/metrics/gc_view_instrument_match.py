from time import time_ns
from typing import Dict, List, Optional, Sequence

from opentelemetry.sdk.metrics._internal._view_instrument_match import (
    _ViewInstrumentMatch as _OrigViewInstrumentMatch,
)
from opentelemetry.sdk.metrics._internal.export import AggregationTemporality
from opentelemetry.sdk.metrics._internal.measurement import Measurement
from opentelemetry.sdk.metrics._internal.point import DataPointT


class _GcViewInstrumentMatch(_OrigViewInstrumentMatch):
    """
    An extension of _ViewInstrumentMatch that adds garbage collection for idle
    metric series (based on attributes). This is useful for preventing memory
    leaks when dealing with high-cardinality metrics.
    """
    # Idle metric series are cleaned up after 20 minutes. This can be adjusted.
    _IDLE_TIMEOUT_NS = 20 * 60 * 1_000_000_000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_used_ns: Dict[frozenset, int] = {}

    def consume_measurement(
            self, measurement: Measurement, should_sample_exemplar: bool = True
    ) -> None:
        """
        Consumes a measurement, aggregates it, and tracks its usage for GC.
        """
        attributes = measurement.attributes or {}
        measurement_for_aggregation = measurement

        if self._view._attribute_keys is not None:
            filtered_attributes = {
                key: value
                for key, value in attributes.items()
                if key in self._view._attribute_keys
            }

            # If attributes were filtered, a new Measurement object must be used
            # for aggregation. This ensures that if an exemplar is recorded, it
            # will have the correct, filtered set of attributes.
            if len(filtered_attributes) != len(attributes):
                attributes = filtered_attributes
                measurement_for_aggregation = Measurement(
                    value=measurement.value,
                    instrument=measurement.instrument,
                    attributes=attributes,
                    time_unix_nano=measurement.time_unix_nano,
                    context=measurement.context,
                )

        aggr_key = frozenset(attributes.items())
        now_ns = time_ns()

        if aggr_key not in self._attributes_aggregation:
            # Let the parent class handle the thread-safe creation of the aggregation.
            super().consume_measurement(
                measurement_for_aggregation,
                should_sample_exemplar,
            )
        else:
            self._attributes_aggregation[aggr_key].aggregate(
                measurement_for_aggregation, should_sample_exemplar
            )
        self._last_used_ns[aggr_key] = now_ns

    def collect(
            self,
            collection_aggregation_temporality: AggregationTemporality,
            collection_start_nanos: int,
    ) -> Optional[Sequence[DataPointT]]:
        """
        Collects all data points for the metric, and garbage collects idle series.
        """
        data_points: List[DataPointT] = []
        now_ns = time_ns()
        to_delete: List[frozenset] = []

        with self._lock:
            # First, collect data points and identify idle series
            for aggr_key, aggregation in self._attributes_aggregation.items():
                dp = aggregation.collect(
                    collection_aggregation_temporality,
                    collection_start_nanos,
                )
                if dp is not None:
                    data_points.append(dp)

                last_used_ns = self._last_used_ns.get(aggr_key, 0)
                if last_used_ns and (now_ns - last_used_ns > self._IDLE_TIMEOUT_NS):
                    to_delete.append(aggr_key)

            # Then, remove the idle series
            for aggr_key in to_delete:
                self._attributes_aggregation.pop(aggr_key, None)
                self._last_used_ns.pop(aggr_key, None)

        return data_points or None


def patch_view_instrument_match() -> None:
    # Call this once at application startup, before initializing any metric
    # readers or providers, to replace the SDK's internal class.
    import opentelemetry.sdk.metrics._internal._view_instrument_match as vim_mod
    vim_mod._ViewInstrumentMatch = _GcViewInstrumentMatch

    from opentelemetry.sdk.metrics._internal import metric_reader_storage as mrs
    mrs._ViewInstrumentMatch = _GcViewInstrumentMatch
