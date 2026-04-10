import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:timeago/timeago.dart' as timeago;

import '../models/status.dart';
import '../providers/api_providers.dart';
import '../widgets/power_flow_card.dart';
import '../widgets/status_badge.dart';

class DashboardScreen extends ConsumerWidget {
  const DashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
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
          data: (status) => _Dashboard(status: status),
        ),
      ),
    );
  }
}

// ── main content ──────────────────────────────────────────────────────────────

class _Dashboard extends StatelessWidget {
  const _Dashboard({required this.status});
  final ChargeStatus status;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        // Timestamp
        if (status.timestamp != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              'Updated ${timeago.format(DateTime.parse(status.timestamp!))}',
              style: const TextStyle(
                color: CupertinoColors.systemGrey,
                fontSize: 12,
              ),
              textAlign: TextAlign.center,
            ),
          ),

        // Status badge
        StatusBadge(status: status),
        const SizedBox(height: 16),

        // Power flow card
        PowerFlowCard(status: status),
        const SizedBox(height: 16),

        // Battery guard card
        _GuardCard(status: status),
        const SizedBox(height: 16),

        // Override card
        _OverrideCard(status: status),
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
        if (status.guardReason.isNotEmpty)
          _Row('Reason', status.guardReason),
        if (status.guardCloudPct != null)
          _Row('Cloud today', '${status.guardCloudPct!.round()} %'),
        if (status.guardTomorrowCloudPct != null)
          _Row('Cloud tomorrow', '${status.guardTomorrowCloudPct!.round()} %'),
      ],
    );
  }
}

// ── override card ─────────────────────────────────────────────────────────────

class _OverrideCard extends ConsumerWidget {
  const _OverrideCard({required this.status});
  final ChargeStatus status;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.read(apiServiceProvider);

    return _Card(
      title: 'Manual Override',
      children: [
        if (status.overrideActive) ...[
          _Row(
            'Active',
            '${status.overrideCurrentA?.toStringAsFixed(1)} A',
          ),
          if (status.overrideUntil != null)
            _Row('Until', status.overrideUntil!),
          CupertinoButton(
            onPressed: () async {
              await api.resumeAuto();
              ref.read(statusProvider.notifier).refresh();
            },
            child: const Text('Resume Auto'),
          ),
        ] else
          _Row('Active', 'No – auto mode'),
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
