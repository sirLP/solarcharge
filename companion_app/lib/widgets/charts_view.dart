import 'dart:math';

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/api_providers.dart';

// ── field palette ─────────────────────────────────────────────────────────────

typedef _FieldDef = ({String key, String label, Color color});

const _kFieldDefs = <_FieldDef>[
  (key: 'solar_w', label: 'Solar', color: Color(0xFFF6AD55)),
  (key: 'surplus_w', label: 'Surplus', color: Color(0xFF48BB78)),
  (key: 'ev_w', label: 'EV Charging', color: Color(0xFF4299E1)),
  (key: 'house_w', label: 'House', color: Color(0xFF9F7AEA)),
];

const _kRanges = <({String label, int minutes})>[
  (label: '1h', minutes: 60),
  (label: '6h', minutes: 360),
  (label: '24h', minutes: 1440),
];

int _chartMinutesCache = 60;
Set<String> _chartVisibleFieldKeysCache = _kFieldDefs.map((f) => f.key).toSet();
Map<String, dynamic>? _chartDataCache;

// ── ChartsView ────────────────────────────────────────────────────────────────

/// Full-screen power-timeseries chart shown when the phone is in landscape.
class ChartsView extends ConsumerStatefulWidget {
  const ChartsView({super.key});

  @override
  ConsumerState<ChartsView> createState() => _ChartsViewState();
}

class _ChartsViewState extends ConsumerState<ChartsView> {
  int _minutes = _chartMinutesCache;
  Future<Map<String, dynamic>>? _future;
  late final Set<String> _visibleFieldKeys;
  Map<String, dynamic>? _cachedData;

  @override
  void initState() {
    super.initState();
    _visibleFieldKeys = {..._chartVisibleFieldKeysCache};
    _cachedData = _chartDataCache;
    _load();
  }

  void _load() {
    _chartMinutesCache = _minutes;
    final api = ref.read(apiServiceProvider);
    _future = api.fetchTimeseries(
      minutes: _minutes,
      fields: _kFieldDefs.map((f) => f.key).join(','),
    );
  }

  void _toggleField(String key) {
    setState(() {
      if (_visibleFieldKeys.contains(key)) {
        _visibleFieldKeys.remove(key);
      } else {
        _visibleFieldKeys.add(key);
      }
      _chartVisibleFieldKeysCache = {..._visibleFieldKeys};
    });
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(
        middle: const Text('Power Graph'),
        trailing: CupertinoButton(
          padding: EdgeInsets.zero,
          onPressed: () => setState(_load),
          child: const Icon(CupertinoIcons.refresh),
        ),
      ),
      child: SafeArea(
        child: Column(
          children: [
            // ── range selector ──────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
              child: CupertinoSlidingSegmentedControl<int>(
                groupValue: _minutes,
                children: {
                  for (final r in _kRanges) r.minutes: Text(r.label),
                },
                onValueChanged: (v) {
                  if (v == null) return;
                  setState(() {
                    _minutes = v;
                    _load();
                  });
                },
              ),
            ),

            // ── chart body ──────────────────────────────────────────────────
            Expanded(
              child: FutureBuilder<Map<String, dynamic>>(
                future: _future,
                builder: (context, snap) {
                  final data = snap.data ?? _cachedData;

                  if (snap.connectionState == ConnectionState.waiting && data == null) {
                    return const Center(child: CupertinoActivityIndicator());
                  }
                  if (snap.hasError && data == null) {
                    return Center(child: Text('Error: ${snap.error}'));
                  }
                  if (data != null) {
                    _cachedData = data;
                    _chartDataCache = data;
                  }
                  return data == null
                      ? const Center(child: CupertinoActivityIndicator())
                      : _ChartBody(
                          data: data,
                          minutes: _minutes,
                          visibleFieldKeys: _visibleFieldKeys,
                          onToggleField: _toggleField,
                        );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── chart body ────────────────────────────────────────────────────────────────

class _ChartBody extends StatelessWidget {
  const _ChartBody({
    required this.data,
    required this.minutes,
    required this.visibleFieldKeys,
    required this.onToggleField,
  });

  final Map<String, dynamic> data;
  final int minutes;
  final Set<String> visibleFieldKeys;
  final ValueChanged<String> onToggleField;

  @override
  Widget build(BuildContext context) {
    final timestamps = (data['timestamps'] as List?)?.cast<String>() ?? [];
    if (timestamps.isEmpty) {
      return const Center(child: Text('No data yet'));
    }

    final fields = data['fields'] as Map<String, dynamic>? ?? {};
    final lines = <LineChartBarData>[];
    double minY = 0;
    double maxY = 100;

    for (final def in _kFieldDefs) {
      if (!visibleFieldKeys.contains(def.key)) continue;
      final vals = (fields[def.key] as List?)?.cast<num>() ?? [];
      if (vals.isEmpty) continue;

      final count = min(vals.length, timestamps.length);
      final spots = <FlSpot>[
        for (int i = 0; i < count; i++) FlSpot(i.toDouble(), vals[i].toDouble()),
      ];

      final localMin = spots.map((s) => s.y).reduce(min);
      final localMax = spots.map((s) => s.y).reduce(max);
      if (localMin < minY) minY = localMin;
      if (localMax > maxY) maxY = localMax;

      lines.add(LineChartBarData(
        spots: spots,
        color: def.color,
        barWidth: 1.5,
        isCurved: true,
        curveSmoothness: 0.2,
        dotData: const FlDotData(show: false),
        belowBarData: BarAreaData(show: false),
      ));
    }

    final hasVisibleLines = lines.isNotEmpty;
    final n = timestamps.length;
    final maxX = hasVisibleLines ? (n - 1).toDouble() : 1.0;
    // ~5 labels across the x-axis
    final xInterval = hasVisibleLines ? max(1.0, (n / 5).roundToDouble()) : 1.0;
    // nice round y-axis increment
    final ySpan = maxY - minY;
    final yInterval = _niceInterval(ySpan);
    final chartMinY = minY < 0 ? minY - yInterval * 0.2 : 0.0;
    final chartMaxY = maxY + yInterval * 0.2;

    return Column(
      children: [
        // ── legend ──────────────────────────────────────────────────────────
        _Legend(
          visibleFieldKeys: visibleFieldKeys,
          onToggleField: onToggleField,
        ),

        // ── line chart ──────────────────────────────────────────────────────
        Expanded(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(4, 4, 16, 8),
            child: hasVisibleLines
                ? LineChart(
                    LineChartData(
                      lineBarsData: lines,
                      minX: 0,
                      maxX: maxX,
                      minY: chartMinY,
                      maxY: chartMaxY,
                      clipData: const FlClipData.all(),
                      titlesData: FlTitlesData(
                        topTitles: const AxisTitles(
                          sideTitles: SideTitles(showTitles: false),
                        ),
                        rightTitles: const AxisTitles(
                          sideTitles: SideTitles(showTitles: false),
                        ),
                        bottomTitles: AxisTitles(
                          sideTitles: SideTitles(
                            showTitles: true,
                            reservedSize: 26,
                            interval: xInterval,
                            getTitlesWidget: (value, meta) {
                              final i = value.round();
                              if (i < 0 || i >= timestamps.length) {
                                return const SizedBox.shrink();
                              }
                              try {
                                final dt = DateTime.parse(timestamps[i]).toLocal();
                                final label = minutes <= 360
                                    ? '${dt.hour.toString().padLeft(2, '0')}:'
                                      '${dt.minute.toString().padLeft(2, '0')}'
                                    : '${dt.month}/${dt.day} '
                                      '${dt.hour.toString().padLeft(2, '0')}h';
                                return SideTitleWidget(
                                  meta: meta,
                                  child: Text(
                                    label,
                                    style: const TextStyle(
                                      fontSize: 9,
                                      color: CupertinoColors.systemGrey,
                                    ),
                                  ),
                                );
                              } catch (_) {
                                return const SizedBox.shrink();
                              }
                            },
                          ),
                        ),
                        leftTitles: AxisTitles(
                          sideTitles: SideTitles(
                            showTitles: true,
                            reservedSize: 50,
                            interval: yInterval,
                            getTitlesWidget: (value, meta) {
                              final label = value.abs() >= 1000
                                  ? '${(value / 1000).toStringAsFixed(1)}k'
                                  : value.toStringAsFixed(0);
                              return SideTitleWidget(
                                meta: meta,
                                child: Text(
                                  label,
                                  style: const TextStyle(
                                    fontSize: 10,
                                    color: CupertinoColors.systemGrey,
                                  ),
                                ),
                              );
                            },
                          ),
                        ),
                      ),
                      gridData: FlGridData(
                        show: true,
                        horizontalInterval: yInterval,
                        getDrawingHorizontalLine: (_) => const FlLine(
                          color: Color(0x22888888),
                          strokeWidth: 0.5,
                        ),
                        getDrawingVerticalLine: (_) => const FlLine(
                          color: Color(0x11888888),
                          strokeWidth: 0.5,
                        ),
                      ),
                      borderData: FlBorderData(
                        show: true,
                        border: Border.all(
                          color: const Color(0x33888888),
                          width: 0.5,
                        ),
                      ),
                    ),
                  )
                : const Center(
                    child: Text(
                      'No visible graph selected',
                      style: TextStyle(color: CupertinoColors.systemGrey),
                    ),
                  ),
          ),
        ),
      ],
    );
  }

  /// Returns a clean interval for the y-axis given the data span.
  static double _niceInterval(double span) {
    if (span <= 0) return 100;
    final exp = (log(span) / log(10)).floor();
    final nicePow = pow(10, exp).toDouble();
    final normalised = span / nicePow;
    final step = normalised < 2 ? 0.5 : normalised < 5 ? 1.0 : 2.0;
    return nicePow * step;
  }
}

// ── legend row ────────────────────────────────────────────────────────────────

class _Legend extends StatelessWidget {
  const _Legend({
    required this.visibleFieldKeys,
    required this.onToggleField,
  });

  final Set<String> visibleFieldKeys;
  final ValueChanged<String> onToggleField;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      child: Wrap(
        alignment: WrapAlignment.center,
        spacing: 12,
        runSpacing: 4,
        children: [
          for (final def in _kFieldDefs)
            CupertinoButton(
              padding: EdgeInsets.zero,
              minimumSize: Size.zero,
              onPressed: () => onToggleField(def.key),
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 10,
                  vertical: 6,
                ),
                decoration: BoxDecoration(
                  color: visibleFieldKeys.contains(def.key)
                      ? def.color.withValues(alpha: 0.12)
                      : CupertinoColors.systemGrey6,
                  border: Border.all(
                    color: visibleFieldKeys.contains(def.key)
                        ? def.color
                        : CupertinoColors.separator,
                  ),
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
                      width: 16,
                      height: 3,
                      decoration: BoxDecoration(
                        color: def.color,
                        borderRadius: BorderRadius.circular(2),
                      ),
                    ),
                    const SizedBox(width: 6),
                    Text(
                      def.label,
                      style: TextStyle(
                        fontSize: 11,
                        color: visibleFieldKeys.contains(def.key)
                            ? CupertinoColors.label
                            : CupertinoColors.systemGrey,
                        fontWeight: visibleFieldKeys.contains(def.key)
                            ? FontWeight.w600
                            : FontWeight.normal,
                      ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}
