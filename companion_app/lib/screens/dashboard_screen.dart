import 'dart:async';

import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/status.dart';
import '../providers/api_providers.dart';
import '../widgets/power_flow_card.dart';
import '../widgets/status_badge.dart';

class DashboardScreen extends ConsumerStatefulWidget {
  const DashboardScreen({super.key});

  @override
  ConsumerState<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends ConsumerState<DashboardScreen>
    with WidgetsBindingObserver {
  Timer? _clockTimer;
  DateTime _now = DateTime.now();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // Tick every second so the elapsed-time label stays current.
    _clockTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      setState(() => _now = DateTime.now());
    });
  }

  @override
  void dispose() {
    _clockTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  /// Fires every time the app transitions back to the foreground.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      ref.read(statusProvider.notifier).refresh();
    }
  }

  String _elapsedLabel(String? timestamp) {
    if (timestamp == null) return 'Updated —';
    final updated = DateTime.parse(timestamp).toLocal();
    final elapsed = _now.difference(updated);
    if (elapsed.inMinutes >= 5) return 'Connection to server may be lost';
    final mm = elapsed.inMinutes.toString().padLeft(2, '0');
    final ss = (elapsed.inSeconds % 60).toString().padLeft(2, '0');
    return 'Updated $mm:$ss ago';
  }

  bool _isStale(String? timestamp) {
    if (timestamp == null) return false;
    final updated = DateTime.parse(timestamp).toLocal();
    return _now.difference(updated).inMinutes >= 5;
  }

  @override
  Widget build(BuildContext context) {
    final statusAsync = ref.watch(statusProvider);

    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(
        middle: const Text('SolarCharge'),
        trailing: CupertinoButton(
          padding: EdgeInsets.zero,
          onPressed: () => ref.read(statusProvider.notifier).refresh(),
          child: const Icon(CupertinoIcons.refresh),
        ),
      ),
      child: SafeArea(
        child: statusAsync.when(
          loading: () => const Center(child: CupertinoActivityIndicator()),
          error: (err, _) => _ErrorView(error: err.toString()),
          data: (status) => _Dashboard(
            status: status,
            elapsedLabel: _elapsedLabel(status.timestamp),
            isStale: _isStale(status.timestamp),
          ),
        ),
      ),
    );
  }
}

// ── main content ──────────────────────────────────────────────────────────────

class _Dashboard extends StatelessWidget {
  const _Dashboard({
    required this.status,
    required this.elapsedLabel,
    required this.isStale,
  });
  final ChargeStatus status;
  final String elapsedLabel;
  final bool isStale;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        // Timestamp
        Padding(
          padding: const EdgeInsets.only(bottom: 8),
          child: Text(
            elapsedLabel,
              style: TextStyle(
                color: isStale
                    ? CupertinoColors.systemRed
                    : CupertinoColors.systemGrey,
                fontSize: 12,
                fontWeight:
                    isStale ? FontWeight.w600 : FontWeight.normal,
              ),
              textAlign: TextAlign.center,
            ),
          ),

        // 1) Charging status
        StatusBadge(status: status),
        const SizedBox(height: 16),

        // 2) Overwrite
        _OverrideCard(status: status),
        const SizedBox(height: 16),

        // 3) Battery guard
        _GuardCard(status: status),
        const SizedBox(height: 16),

        // 4) Power flow tiles
        PowerFlowCard(status: status),
      ],
    );
  }
}

// ── guard card ───────────────────────────────────────────────────────────────

class _GuardCard extends StatelessWidget {
  const _GuardCard({required this.status});
  final ChargeStatus status;

  @override
  Widget build(BuildContext context) {
    return _Card(
      title: 'Battery Guard',
      children: [
        _Row('Enabled', status.guardEnabled ? 'Yes' : 'No'),
        _Row(
          'Active',
          status.guardActive ? '${(status.guardFactor * 100).round()} %' : 'No',
        ),
        _Row('Battery SoC', '${status.batterySocPct.toStringAsFixed(1)} %'),
        _Row('Required SoC', '${status.guardRequiredSoc.toStringAsFixed(1)} %'),
        if (status.guardCloudPct != null)
          _Row('Cloud today', '${status.guardCloudPct!.round()} %'),
        if (status.guardTomorrowCloudPct != null)
          _Row('Cloud tomorrow', '${status.guardTomorrowCloudPct!.round()} %'),
        if (status.guardReason.isNotEmpty) ...[
          const SizedBox(height: 8),
          const Text(
            'Reason',
            style: TextStyle(
              color: CupertinoColors.systemGrey,
              fontSize: 12,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            status.guardReason,
            style: const TextStyle(fontWeight: FontWeight.w500),
            softWrap: true,
          ),
        ],
      ],
    );
  }
}

// ── overwrite card ────────────────────────────────────────────────────────────

class _OverrideCard extends ConsumerWidget {
  const _OverrideCard({required this.status});
  final ChargeStatus status;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.read(apiServiceProvider);

    return _Card(
      title: 'Manual Overwrite',
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            const Text(
              'Enable Overwrite',
              style: TextStyle(color: CupertinoColors.systemGrey),
            ),
            CupertinoSwitch(
              value: status.overrideActive,
              onChanged: (enabled) async {
                if (enabled) {
                  await api.setOverride(currentA: 16.0);
                } else {
                  await api.resumeAuto();
                }
                ref.read(statusProvider.notifier).refresh();
              },
            ),
          ],
        ),
        const SizedBox(height: 8),
        if (status.overrideActive) ...[
          _Row('Current', '${status.overrideCurrentA?.toStringAsFixed(1)} A'),
          if (status.overrideUntil != null) _Row('Expires', status.overrideUntil!),
        ] else
          _Row('State', 'Off - automatic mode'),
      ],
    );
  }
}

// ── small shared widgets ──────────────────────────────────────────────────────

class _Card extends StatelessWidget {
  const _Card({required this.title, required this.children});
  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: CupertinoColors.secondarySystemGroupedBackground,
        borderRadius: BorderRadius.circular(12),
      ),
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(
              fontWeight: FontWeight.w600,
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

class _Row extends StatelessWidget {
  const _Row(this.label, this.value);
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label,
              style: const TextStyle(color: CupertinoColors.systemGrey)),
          Text(value, style: const TextStyle(fontWeight: FontWeight.w500)),
        ],
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.error});
  final String error;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(
              CupertinoIcons.exclamationmark_circle,
              size: 48,
              color: CupertinoColors.systemRed,
            ),
            const SizedBox(height: 12),
            const Text(
              'Could not reach SolarCharge',
              style: TextStyle(fontWeight: FontWeight.w600, fontSize: 16),
            ),
            const SizedBox(height: 8),
            Text(
              error,
              style: const TextStyle(
                color: CupertinoColors.systemGrey,
                fontSize: 12,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            const Text(
              'Check the server URL in Settings.',
              style: TextStyle(color: CupertinoColors.systemGrey),
            ),
          ],
        ),
      ),
    );
  }
}
