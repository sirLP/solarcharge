import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/api_providers.dart';

_HistoryPayload? _historyPayloadCache;
String _historyActivePresetCache = 'all';
String _historySelectedRfidTagCache = '';

class ChargingHistoryScreen extends ConsumerStatefulWidget {
  const ChargingHistoryScreen({super.key});

  @override
  ConsumerState<ChargingHistoryScreen> createState() =>
      _ChargingHistoryScreenState();
}

class _ChargingHistoryScreenState extends ConsumerState<ChargingHistoryScreen> {
  late Future<_HistoryPayload> _future;
  _HistoryPayload? _cachedData;
  String _activePreset = _historyActivePresetCache;
  String _selectedRfidTag = _historySelectedRfidTagCache;

  @override
  void initState() {
    super.initState();
    _cachedData = _historyPayloadCache;
    _future = _load();
  }

  Future<_HistoryPayload> _load() async {
    final api = ref.read(apiServiceProvider);
    final body = await api.fetchWallboxSessions();
    final sessions = (body['sessions'] as List?)
            ?.cast<Map<String, dynamic>>()
            .map(_ChargingSession.fromJson)
            .toList() ??
        const [];
    final payload = _HistoryPayload(
      sessions: sessions,
      error: body['error'] as String?,
      fetchedAt: DateTime.now(),
    );
    _historyPayloadCache = payload;
    _cachedData = payload;
    return payload;
  }

  Future<void> _refresh() async {
    setState(() {
      _future = _load();
    });
  }

  Future<void> _selectRfidTag(List<String> tags) async {
    final value = await showCupertinoModalPopup<String>(
      context: context,
      builder: (context) => CupertinoActionSheet(
        title: const Text('RFID Filter'),
        actions: [
          CupertinoActionSheetAction(
            onPressed: () => Navigator.of(context).pop(''),
            child: Text('All RFID tags (${tags.length})'),
          ),
          for (final tag in tags)
            CupertinoActionSheetAction(
              onPressed: () => Navigator.of(context).pop(tag),
              child: Text(tag),
            ),
        ],
        cancelButton: CupertinoActionSheetAction(
          onPressed: () => Navigator.of(context).pop(),
          isDefaultAction: true,
          child: const Text('Cancel'),
        ),
      ),
    );

    if (value == null) return;
    setState(() {
      _selectedRfidTag = value;
      _historySelectedRfidTagCache = value;
    });
  }

  void _setPreset(String preset) {
    setState(() {
      _activePreset = preset;
      _historyActivePresetCache = preset;
    });
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(
        middle: const Text('Charging History'),
        trailing: CupertinoButton(
          padding: EdgeInsets.zero,
          onPressed: _refresh,
          child: const Icon(CupertinoIcons.refresh),
        ),
      ),
      child: SafeArea(
        child: FutureBuilder<_HistoryPayload>(
          future: _future,
          builder: (context, snap) {
            final payload = snap.data ?? _cachedData;
            final isRefreshing =
                snap.connectionState == ConnectionState.waiting && _cachedData != null;

            if (snap.connectionState == ConnectionState.waiting && payload == null) {
              return const Center(child: CupertinoActivityIndicator());
            }
            if (snap.hasError && payload == null) {
              return Center(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text('Could not load charging history: ${snap.error}'),
                ),
              );
            }

            if (payload == null) {
              return const Center(child: Text('No charging history available.'));
            }

            final sessions = payload.sessions;
            final tags = sessions
                .map((session) => session.rfidTag)
                .where((tag) => tag.isNotEmpty)
                .toSet()
                .toList()
              ..sort();
            final visible = _filterSessions(sessions);
            final completed = visible
                .where((session) =>
                    session.status == 'completed' && session.energyKwh != null)
                .toList();
            final totalEnergy = completed.fold<double>(
              0,
              (sum, session) => sum + (session.energyKwh ?? 0),
            );
            final totalDuration = completed.fold<int>(
              0,
              (sum, session) => sum + (session.durationS ?? 0),
            );
            final avgEnergy = completed.isEmpty
                ? null
                : totalEnergy / completed.length;
            final hasBadClock = sessions.any(
              (session) =>
                  session.startedAt != null && session.startedAt!.year < 2020,
            );

            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                if (payload.error case final error?) ...[
                  _MessageCard(
                    message: 'Wallbox error: $error',
                    color: CupertinoColors.systemRed,
                    background: const Color(0xFFFDEDED),
                  ),
                  const SizedBox(height: 12),
                ],
                if (snap.hasError) ...[
                  _MessageCard(
                    message:
                        'Refresh failed. Showing the last successfully retrieved charging history.',
                    color: CupertinoColors.systemOrange,
                    background: const Color(0xFFFFF7D6),
                  ),
                  const SizedBox(height: 12),
                ],
                if (hasBadClock) ...[
                  const _MessageCard(
                    message:
                        'Some sessions show dates before 2020. The wallbox clock was not synchronised when those sessions occurred.',
                    color: CupertinoColors.systemOrange,
                    background: Color(0xFFFFF7D6),
                  ),
                  const SizedBox(height: 12),
                ],
                _Card(
                  title: 'Filters',
                  children: [
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        for (final preset in _kPresets)
                          _FilterPill(
                            label: preset.label,
                            selected: _activePreset == preset.key,
                            onPressed: () => _setPreset(preset.key),
                          ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    _FilterButton(
                      label: 'RFID Tag',
                      value: _selectedRfidTag.isEmpty
                          ? 'All RFID tags'
                          : _selectedRfidTag,
                      onPressed: tags.isEmpty ? null : () => _selectRfidTag(tags),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      isRefreshing
                          ? 'Refreshing in background... showing cached data from ${_fmtTime(payload.fetchedAt)}'
                          : 'Fetched at ${_fmtTime(payload.fetchedAt)}',
                      style: const TextStyle(
                        color: CupertinoColors.systemGrey,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 12,
                  runSpacing: 12,
                  children: [
                    _SummaryCard(label: 'Sessions', value: '${visible.length}'),
                    _SummaryCard(
                      label: 'Total Energy',
                      value: totalEnergy.toStringAsFixed(2),
                      unit: 'kWh',
                    ),
                    _SummaryCard(
                      label: 'Avg per Session',
                      value: avgEnergy?.toStringAsFixed(2) ?? '—',
                      unit: 'kWh',
                    ),
                    _SummaryCard(
                      label: 'Total Charge Time',
                      value: totalDuration == 0
                          ? '—'
                          : (totalDuration / 3600).toStringAsFixed(1),
                      unit: 'hours',
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                _Card(
                  title: 'Sessions',
                  children: [
                    if (visible.isEmpty)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 28),
                        child: Center(
                          child: Text(
                            'No sessions found for the selected range.',
                            style: TextStyle(color: CupertinoColors.systemGrey),
                          ),
                        ),
                      )
                    else
                      SingleChildScrollView(
                        scrollDirection: Axis.horizontal,
                        child: ConstrainedBox(
                          constraints: const BoxConstraints(minWidth: 980),
                          child: Column(
                            children: [
                              const _HistoryHeaderRow(),
                              for (final session in visible)
                                _HistoryDataRow(session: session),
                            ],
                          ),
                        ),
                      ),
                  ],
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  List<_ChargingSession> _filterSessions(List<_ChargingSession> sessions) {
    final range = _activeRange();
    final start = range.$1;
    final end = range.$2;
    final filtered = sessions.where((session) {
      final date = session.startedAt;
      if (date != null && start != null && date.isBefore(start)) {
        return false;
      }
      if (date != null && end != null && date.isAfter(end)) {
        return false;
      }
      if (_selectedRfidTag.isNotEmpty && session.rfidTag != _selectedRfidTag) {
        return false;
      }
      return true;
    }).toList()
      ..sort((a, b) {
        final aDate = a.startedAt ?? DateTime.fromMillisecondsSinceEpoch(0);
        final bDate = b.startedAt ?? DateTime.fromMillisecondsSinceEpoch(0);
        return bDate.compareTo(aDate);
      });
    return filtered;
  }

  (DateTime?, DateTime?) _activeRange() {
    final now = _dateOnly(DateTime.now());
    switch (_activePreset) {
      case 'today':
        return (now, now.add(const Duration(days: 1)).subtract(const Duration(milliseconds: 1)));
      case 'week':
        final dowMon = (now.weekday + 6) % 7;
        final thisMonday = now.subtract(Duration(days: dowMon));
        final lastMonday = thisMonday.subtract(const Duration(days: 7));
        final lastSunday = thisMonday.subtract(const Duration(days: 1));
        return (lastMonday, _endOfDay(lastSunday));
      case 'month':
        final start = DateTime(now.year, now.month - 1, 1);
        final end = DateTime(now.year, now.month, 0);
        return (start, _endOfDay(end));
      case 'year':
        final y = now.year - 1;
        return (DateTime(y, 1, 1), _endOfDay(DateTime(y, 12, 31)));
      default:
        return (null, null);
    }
  }
}

class _HistoryPayload {
  const _HistoryPayload({
    required this.sessions,
    required this.error,
    required this.fetchedAt,
  });

  final List<_ChargingSession> sessions;
  final String? error;
  final DateTime fetchedAt;
}

class _ChargingSession {
  const _ChargingSession({
    required this.id,
    required this.startedAt,
    required this.endedAt,
    required this.durationS,
    required this.energyKwh,
    required this.startMeterKwh,
    required this.stopMeterKwh,
    required this.rfidTag,
    required this.status,
  });

  factory _ChargingSession.fromJson(Map<String, dynamic> json) {
    return _ChargingSession(
      id: (json['id'] as num?)?.toInt() ?? 0,
      startedAt: _parseDateTime(json['started_at']),
      endedAt: _parseDateTime(json['ended_at']),
      durationS: (json['duration_s'] as num?)?.toInt(),
      energyKwh: (json['energy_kwh'] as num?)?.toDouble(),
      startMeterKwh: (json['start_meter_kwh'] as num?)?.toDouble(),
      stopMeterKwh: (json['stop_meter_kwh'] as num?)?.toDouble(),
      rfidTag: (json['rfid_tag'] as String? ?? '').trim(),
      status: (json['status'] as String? ?? '').trim(),
    );
  }

  final int id;
  final DateTime? startedAt;
  final DateTime? endedAt;
  final int? durationS;
  final double? energyKwh;
  final double? startMeterKwh;
  final double? stopMeterKwh;
  final String rfidTag;
  final String status;
}

class _Card extends StatelessWidget {
  const _Card({required this.title, required this.children});

  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: CupertinoColors.white,
        border: Border.all(
          color: CupertinoColors.separator,
          width: 1.2,
        ),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(
              fontWeight: FontWeight.w700,
              fontSize: 16,
            ),
          ),
          const SizedBox(height: 8),
          ...children,
        ],
      ),
    );
  }
}

class _MessageCard extends StatelessWidget {
  const _MessageCard({
    required this.message,
    required this.color,
    required this.background,
  });

  final String message;
  final Color color;
  final Color background;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: color.withValues(alpha: 0.45)),
      ),
      child: Text(
        message,
        style: TextStyle(color: color, fontSize: 13),
      ),
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({
    required this.label,
    required this.value,
    this.unit,
  });

  final String label;
  final String value;
  final String? unit;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 180,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: CupertinoColors.white,
        border: Border.all(color: CupertinoColors.separator, width: 1.2),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              color: CupertinoColors.systemGrey,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            value,
            style: const TextStyle(
              fontWeight: FontWeight.w700,
              fontSize: 24,
            ),
          ),
          if (unit != null) ...[
            const SizedBox(height: 2),
            Text(
              unit!,
              style: const TextStyle(
                color: CupertinoColors.systemGrey,
                fontSize: 12,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _FilterPill extends StatelessWidget {
  const _FilterPill({
    required this.label,
    required this.selected,
    required this.onPressed,
  });

  final String label;
  final bool selected;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return CupertinoButton(
      padding: EdgeInsets.zero,
      minimumSize: Size.zero,
      onPressed: onPressed,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: selected
              ? CupertinoColors.label
              : CupertinoColors.systemGrey6,
          borderRadius: BorderRadius.circular(999),
          border: Border.all(
            color: selected
                ? CupertinoColors.label
                : CupertinoColors.separator,
          ),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: selected
                ? CupertinoColors.white
                : CupertinoColors.label,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}

class _FilterButton extends StatelessWidget {
  const _FilterButton({
    required this.label,
    required this.value,
    required this.onPressed,
  });

  final String label;
  final String value;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) {
    return CupertinoButton(
      padding: EdgeInsets.zero,
      minimumSize: Size.zero,
      onPressed: onPressed,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: CupertinoColors.systemGrey6,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: CupertinoColors.separator),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              label,
              style: const TextStyle(
                color: CupertinoColors.systemGrey,
                fontSize: 11,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              value,
              style: TextStyle(
                color: onPressed == null
                    ? CupertinoColors.systemGrey2
                    : CupertinoColors.label,
                fontSize: 13,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _HistoryHeaderRow extends StatelessWidget {
  const _HistoryHeaderRow();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 8),
      decoration: const BoxDecoration(
        border: Border(
          bottom: BorderSide(color: CupertinoColors.separator, width: 1),
        ),
      ),
      child: const Row(
        children: [
          _HeaderCell('ID', 44),
          _HeaderCell('Started', 140),
          _HeaderCell('Ended', 140),
          _HeaderCell('Duration', 90),
          _HeaderCell('Energy', 100),
          _HeaderCell('Meter reading', 170),
          _HeaderCell('RFID Tag', 130),
          _HeaderCell('Status', 110),
        ],
      ),
    );
  }
}

class _HistoryDataRow extends StatelessWidget {
  const _HistoryDataRow({required this.session});

  final _ChargingSession session;

  @override
  Widget build(BuildContext context) {
    final isCompleted = session.status == 'completed';

    return Container(
      padding: const EdgeInsets.symmetric(vertical: 10),
      decoration: const BoxDecoration(
        border: Border(
          bottom: BorderSide(color: CupertinoColors.separator, width: 0.5),
        ),
      ),
      child: Row(
        children: [
          _ValueCell('${session.id}', 44),
          _ValueCell(_fmtDateTime(session.startedAt), 140),
          _ValueCell(_fmtDateTime(session.endedAt), 140),
          _ValueCell(_fmtDuration(session.durationS), 90),
          _ValueCell(session.energyKwh?.toStringAsFixed(3) ?? '—', 100,
              weight: FontWeight.w600),
          _ValueCell(
            _fmtMeter(session.startMeterKwh, session.stopMeterKwh),
            170,
            color: CupertinoColors.systemGrey,
            fontSize: 12,
          ),
          _ValueCell(session.rfidTag.isEmpty ? '—' : session.rfidTag, 130,
              color: session.rfidTag.isEmpty
                  ? CupertinoColors.systemGrey
                  : const Color(0xFF553C9A)),
          SizedBox(
            width: 110,
            child: Align(
              alignment: Alignment.centerLeft,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(
                  color: isCompleted
                      ? const Color(0xFFD7F5DD)
                      : const Color(0xFFDDEEFF),
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Text(
                  isCompleted ? 'Completed' : 'In progress',
                  style: TextStyle(
                    color: isCompleted
                        ? const Color(0xFF22543D)
                        : const Color(0xFF2A4365),
                    fontSize: 11,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _HeaderCell extends StatelessWidget {
  const _HeaderCell(this.label, this.width);

  final String label;
  final double width;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: width,
      child: Text(
        label,
        style: const TextStyle(
          color: CupertinoColors.systemGrey,
          fontSize: 11,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

class _ValueCell extends StatelessWidget {
  const _ValueCell(
    this.value,
    this.width, {
    this.color = CupertinoColors.label,
    this.fontSize = 13,
    this.weight = FontWeight.w500,
  });

  final String value;
  final double width;
  final Color color;
  final double fontSize;
  final FontWeight weight;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: width,
      child: Text(
        value,
        style: TextStyle(
          color: color,
          fontSize: fontSize,
          fontWeight: weight,
        ),
      ),
    );
  }
}

class _PresetDef {
  const _PresetDef(this.key, this.label);

  final String key;
  final String label;
}

const _kPresets = <_PresetDef>[
  _PresetDef('today', 'Today'),
  _PresetDef('week', 'Last week'),
  _PresetDef('month', 'Last month'),
  _PresetDef('year', 'Last year'),
  _PresetDef('all', 'All time'),
];

DateTime? _parseDateTime(Object? value) {
  final text = value as String?;
  if (text == null || text.isEmpty) return null;
  return DateTime.tryParse(text);
}

DateTime _dateOnly(DateTime value) => DateTime(value.year, value.month, value.day);

DateTime _endOfDay(DateTime value) =>
    DateTime(value.year, value.month, value.day, 23, 59, 59, 999);

String _fmtTime(DateTime value) {
  final hour = value.hour.toString().padLeft(2, '0');
  final minute = value.minute.toString().padLeft(2, '0');
  final second = value.second.toString().padLeft(2, '0');
  return '$hour:$minute:$second';
}

String _fmtDateTime(DateTime? value) {
  if (value == null) return '—';
  final month = value.month.toString().padLeft(2, '0');
  final day = value.day.toString().padLeft(2, '0');
  final hour = value.hour.toString().padLeft(2, '0');
  final minute = value.minute.toString().padLeft(2, '0');
  return '${value.year}-$month-$day $hour:$minute';
}

String _fmtDuration(int? seconds) {
  if (seconds == null) return '—';
  final hours = seconds ~/ 3600;
  final minutes = (seconds % 3600) ~/ 60;
  final secs = seconds % 60;
  if (hours > 0) return '${hours}h ${minutes}m';
  if (minutes > 0) return '${minutes}m ${secs}s';
  return '${secs}s';
}

String _fmtMeter(double? start, double? stop) {
  final a = start?.toStringAsFixed(3) ?? '?';
  final b = stop?.toStringAsFixed(3) ?? '…';
  return '$a -> $b kWh';
}