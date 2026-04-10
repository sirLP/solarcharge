// Status model matching the backend's /api/status response.
// Run `dart run build_runner build` to regenerate status.g.dart.

import 'package:json_annotation/json_annotation.dart';

part 'status.g.dart';

@JsonSerializable()
class ChargeStatus {
  final String? timestamp;
  @JsonKey(name: 'calc_only')
  final bool calcOnly;

  // SENEC fields
  @JsonKey(name: 'solar_w')
  final double solarW;
  @JsonKey(name: 'grid_w')
  final double gridW;
  @JsonKey(name: 'battery_w')
  final double batteryW;
  @JsonKey(name: 'house_w')
  final double houseW;
  @JsonKey(name: 'battery_soc_pct')
  final double batterySocPct;

  // Derived
  @JsonKey(name: 'surplus_w')
  final double surplusW;
  @JsonKey(name: 'target_a')
  final double targetA;
  @JsonKey(name: 'setpoint_a')
  final double setpointA;
  @JsonKey(name: 'charging_active')
  final bool chargingActive;

  // Alfen
  @JsonKey(name: 'car_status')
  final String carStatus;
  @JsonKey(name: 'car_status_raw')
  final String carStatusRaw;
  @JsonKey(name: 'wallbox_power_w')
  final double wallboxPowerW;

  // Override
  @JsonKey(name: 'override_active')
  final bool overrideActive;
  @JsonKey(name: 'override_current_a')
  final double? overrideCurrentA;
  @JsonKey(name: 'override_until')
  final String? overrideUntil;

  // Session
  @JsonKey(name: 'session_kwh')
  final double sessionKwh;

  // Battery guard
  @JsonKey(name: 'guard_enabled')
  final bool guardEnabled;
  @JsonKey(name: 'guard_active')
  final bool guardActive;
  @JsonKey(name: 'guard_factor')
  final double guardFactor;
  @JsonKey(name: 'guard_linear_mode')
  final bool guardLinearMode;
  @JsonKey(name: 'guard_required_soc')
  final double guardRequiredSoc;
  @JsonKey(name: 'guard_sunset')
  final String guardSunset;
  @JsonKey(name: 'guard_sunrise')
  final String guardSunrise;
  @JsonKey(name: 'guard_reason')
  final String guardReason;
  @JsonKey(name: 'guard_cloud_pct')
  final double? guardCloudPct;
  @JsonKey(name: 'guard_tomorrow_cloud_pct')
  final double? guardTomorrowCloudPct;
  @JsonKey(name: 'guard_tomorrow_boost')
  final double guardTomorrowBoost;
  @JsonKey(name: 'guard_seasonal_extra')
  final double guardSeasonalExtra;

  const ChargeStatus({
    required this.timestamp,
    required this.calcOnly,
    required this.solarW,
    required this.gridW,
    required this.batteryW,
    required this.houseW,
    required this.batterySocPct,
    required this.surplusW,
    required this.targetA,
    required this.setpointA,
    required this.chargingActive,
    required this.carStatus,
    required this.carStatusRaw,
    required this.wallboxPowerW,
    required this.overrideActive,
    this.overrideCurrentA,
    this.overrideUntil,
    required this.sessionKwh,
    required this.guardEnabled,
    required this.guardActive,
    required this.guardFactor,
    required this.guardLinearMode,
    required this.guardRequiredSoc,
    required this.guardSunset,
    required this.guardSunrise,
    required this.guardReason,
    this.guardCloudPct,
    this.guardTomorrowCloudPct,
    required this.guardTomorrowBoost,
    required this.guardSeasonalExtra,
  });

  factory ChargeStatus.fromJson(Map<String, dynamic> json) =>
      _$ChargeStatusFromJson(json);

  Map<String, dynamic> toJson() => _$ChargeStatusToJson(this);
}
