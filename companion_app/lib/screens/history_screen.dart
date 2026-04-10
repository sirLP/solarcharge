import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/api_providers.dart';

class HistoryScreen extends ConsumerStatefulWidget {
  const HistoryScreen({super.key});

  @override
  ConsumerState<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends ConsumerState<HistoryScreen> {
  int _days = 7;
  late Future<List<Map<String, dynamic>>> _historyFuture;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    _historyFuture =
        ref.read(apiServiceProvider).fetchHistory(days: _days);
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      navigationBar: const CupertinoNavigationBar(
        middle: Text('History'),
      ),
      child: SafeArea(
        child: Column(
          children: [
            // Day-range picker
            Padding(
              padding:
                  const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: CupertinoSlidingSegmentedControl<int>(
                groupValue: _days,
                children: const {
                  7: Text('7 d'),
                  14: Text('14 d'),
                  30: Text('30 d'),
                },
                onValueChanged: (v) {
                  if (v == null) return;
                  setState(() {
                    _days = v;
                    _load();
                  });
                },
              ),
            ),
            Expanded(
              child: FutureBuilder<List<Map<String, dynamic>>>(
                future: _historyFuture,
                builder: (context, snap) {
                  if (snap.connectionState == ConnectionState.waiting) {
                    return const Center(child: CupertinoActivityIndicator());
                  }
                  if (snap.hasError) {
                    return Center(child: Text('Error: ${snap.error}'));
                  }
                  final rows = snap.data ?? [];
                  if (rows.isEmpty) {
                    return const Center(child: Text('No history data.'));
                  }
                  return ListView.separated(
                    padding: const EdgeInsets.all(16),
                    itemCount: rows.length,
                    separatorBuilder: (_, _) =>
                        const SizedBox(height: 1),
                    itemBuilder: (context, i) {
                      final row = rows[i];
                      final date = row['date'] as String? ?? '—';
                      final kwh = (row['kwh'] as num?)?.toStringAsFixed(2) ?? '—';
                      final sessions =
                          (row['sessions'] as num?)?.toString() ?? '—';
                      return Container(
                        padding: const EdgeInsets.symmetric(
                            vertical: 10, horizontal: 12),
                        decoration: BoxDecoration(
                          color: CupertinoColors
                              .secondarySystemGroupedBackground,
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Row(
                          children: [
                            Expanded(
                              child: Text(date,
                                  style: const TextStyle(
                                      fontWeight: FontWeight.w500)),
                            ),
                            Text('$kwh kWh',
                                style: const TextStyle(
                                    color: CupertinoColors.systemGreen)),
                            const SizedBox(width: 12),
                            Text('$sessions sessions',
                                style: const TextStyle(
                                    color: CupertinoColors.systemGrey,
                                    fontSize: 12)),
                          ],
                        ),
                      );
                    },
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
