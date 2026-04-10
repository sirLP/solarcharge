import 'package:flutter/cupertino.dart';

import '../models/status.dart';

/// A large status badge at the top of the dashboard showing the car connection
/// state and the current charging rate.
class StatusBadge extends StatelessWidget {
  const StatusBadge({super.key, required this.status});
  final ChargeStatus status;

  @override
  Widget build(BuildContext context) {
    final (label, color, icon) = _statusInfo;

    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        border: Border.all(color: color, width: 1.5),
        borderRadius: BorderRadius.circular(12),
      ),
      padding: const EdgeInsets.all(16),
      child: Row(
        children: [
          Icon(icon, color: color, size: 36),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  style: TextStyle(
                    color: color,
                    fontWeight: FontWeight.w700,
                    fontSize: 18,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  _subtitle,
                  style: const TextStyle(
                    color: CupertinoColors.systemGrey,
                    fontSize: 13,
                  ),
                ),
              ],
            ),
          ),
          if (status.overrideActive)
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: CupertinoColors.systemOrange.withValues(alpha: 0.2),
                borderRadius: BorderRadius.circular(6),
                border: Border.all(
                    color: CupertinoColors.systemOrange, width: 1),
              ),
              child: const Text(
                'OVERRIDE',
                style: TextStyle(
                  color: CupertinoColors.systemOrange,
                  fontSize: 10,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
        ],
      ),
    );
  }

  (String, Color, IconData) get _statusInfo {
    if (status.chargingActive) {
      return (
        'Charging',
        CupertinoColors.systemGreen,
        CupertinoIcons.bolt_fill,
      );
    }
    switch (status.carStatus.toLowerCase()) {
      case 'connected':
        return (
          'Car Connected',
          CupertinoColors.systemBlue,
          CupertinoIcons.car,
        );
      case 'disconnected':
      default:
        return (
          'No Car',
          CupertinoColors.systemGrey,
          CupertinoIcons.bolt_slash,
        );
    }
  }

  String get _subtitle {
    if (status.chargingActive) {
      final kw = (status.wallboxPowerW / 1000).toStringAsFixed(2);
      final a = status.setpointA.toStringAsFixed(1);
      return '$kw kW  ·  $a A  ·  ${status.sessionKwh.toStringAsFixed(2)} kWh session';
    }
    if (status.carStatus.toLowerCase() == 'connected') {
      return 'Waiting for sufficient solar surplus';
    }
    final soc = status.batterySocPct.toStringAsFixed(1);
    return 'Battery $soc %  ·  ${status.carStatus}';
  }
}
