import 'package:flutter/cupertino.dart';

import '../models/status.dart';

/// A card that shows the real-time power flow:
/// solar → house, solar → wallbox, grid import/export, battery in/out.
class PowerFlowCard extends StatelessWidget {
  const PowerFlowCard({super.key, required this.status});
  final ChargeStatus status;

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
          const Text(
            'Power Flow',
            style: TextStyle(fontWeight: FontWeight.w600, fontSize: 16),
          ),
          const SizedBox(height: 12),
          _PowerRow(
            icon: CupertinoIcons.sun_max_fill,
            color: CupertinoColors.systemYellow,
            label: 'Solar',
            valueW: status.solarW,
          ),
          _PowerRow(
            icon: CupertinoIcons.house_fill,
            color: CupertinoColors.systemBlue,
            label: 'House',
            valueW: status.houseW,
          ),
          _PowerRow(
            icon: CupertinoIcons.car_fill,
            color: CupertinoColors.systemGreen,
            label: 'Wallbox',
            valueW: status.wallboxPowerW,
            suffix: status.chargingActive
                ? ' · ${status.setpointA.toStringAsFixed(1)} A'
                : null,
          ),
          _PowerRow(
            icon: CupertinoIcons.battery_100,
            color: CupertinoColors.systemTeal,
            label: 'Battery',
            valueW: status.batteryW,
            invertSign: true, // positive = charging in backend convention
          ),
          _PowerRow(
            icon: CupertinoIcons.antenna_radiowaves_left_right,
            color: status.gridW > 0
                ? CupertinoColors.systemRed
                : CupertinoColors.systemGreen,
            label: status.gridW > 0 ? 'Grid import' : 'Grid export',
            valueW: status.gridW.abs(),
          ),
          Container(height: 0.5, margin: const EdgeInsets.symmetric(vertical: 8), color: CupertinoColors.separator),
          _PowerRow(
            icon: CupertinoIcons.arrow_right_circle_fill,
            color: CupertinoColors.systemGreen,
            label: 'Surplus',
            valueW: status.surplusW,
          ),
          if (status.sessionKwh > 0)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: _PowerRow(
                icon: CupertinoIcons.gauge,
                color: CupertinoColors.systemIndigo,
                label: 'Session',
                valueW: null,
                customValue: '${status.sessionKwh.toStringAsFixed(2)} kWh',
              ),
            ),
        ],
      ),
    );
  }
}

class _PowerRow extends StatelessWidget {
  const _PowerRow({
    required this.icon,
    required this.color,
    required this.label,
    this.valueW,
    this.invertSign = false,
    this.suffix,
    this.customValue,
  });

  final IconData icon;
  final Color color;
  final String label;
  final double? valueW;
  final bool invertSign;
  final String? suffix;
  final String? customValue;

  String get _displayValue {
    if (customValue != null) return customValue!;
    if (valueW == null) return '—';
    final w = invertSign ? -valueW! : valueW!;
    if (w.abs() >= 1000) {
      return '${(w / 1000).toStringAsFixed(2)} kW${suffix ?? ''}';
    }
    return '${w.toStringAsFixed(0)} W${suffix ?? ''}';
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Icon(icon, color: color, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              label,
              style:
                  const TextStyle(color: CupertinoColors.systemGrey),
            ),
          ),
          Text(
            _displayValue,
            style: const TextStyle(fontWeight: FontWeight.w500),
          ),
        ],
      ),
    );
  }
}
