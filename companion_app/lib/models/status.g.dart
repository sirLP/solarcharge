// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'status.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

ChargeStatus _$ChargeStatusFromJson(Map<String, dynamic> json) => ChargeStatus(
  timestamp: json['timestamp'] as String?,
  calcOnly: json['calc_only'] as bool,
  solarW: (json['solar_w'] as num).toDouble(),
  gridW: (json['grid_w'] as num).toDouble(),
  batteryW: (json['battery_w'] as num).toDouble(),
  houseW: (json['house_w'] as num).toDouble(),
  batterySocPct: (json['battery_soc_pct'] as num).toDouble(),
  surplusW: (json['surplus_w'] as num).toDouble(),
  targetA: (json['target_a'] as num).toDouble(),
  setpointA: (json['setpoint_a'] as num).toDouble(),
  chargingActive: json['charging_active'] as bool,
  carStatus: json['car_status'] as String,
  carStatusRaw: json['car_status_raw'] as String,
  wallboxPowerW: (json['wallbox_power_w'] as num).toDouble(),
  overrideActive: json['override_active'] as bool,
  overrideCurrentA: (json['override_current_a'] as num?)?.toDouble(),
  overrideUntil: json['override_until'] as String?,
  sessionKwh: (json['session_kwh'] as num).toDouble(),
  guardEnabled: json['guard_enabled'] as bool,
  guardActive: json['guard_active'] as bool,
  guardFactor: (json['guard_factor'] as num).toDouble(),
  guardLinearMode: json['guard_linear_mode'] as bool,
  guardRequiredSoc: (json['guard_required_soc'] as num).toDouble(),
  guardSunset: json['guard_sunset'] as String,
  guardSunrise: json['guard_sunrise'] as String,
  guardReason: json['guard_reason'] as String,
  guardCloudPct: (json['guard_cloud_pct'] as num?)?.toDouble(),
  guardTomorrowCloudPct: (json['guard_tomorrow_cloud_pct'] as num?)?.toDouble(),
  guardTomorrowBoost: (json['guard_tomorrow_boost'] as num).toDouble(),
  guardSeasonalExtra: (json['guard_seasonal_extra'] as num).toDouble(),
);

Map<String, dynamic> _$ChargeStatusToJson(ChargeStatus instance) =>
    <String, dynamic>{
      'timestamp': instance.timestamp,
      'calc_only': instance.calcOnly,
      'solar_w': instance.solarW,
      'grid_w': instance.gridW,
      'battery_w': instance.batteryW,
      'house_w': instance.houseW,
      'battery_soc_pct': instance.batterySocPct,
      'surplus_w': instance.surplusW,
      'target_a': instance.targetA,
      'setpoint_a': instance.setpointA,
      'charging_active': instance.chargingActive,
      'car_status': instance.carStatus,
      'car_status_raw': instance.carStatusRaw,
      'wallbox_power_w': instance.wallboxPowerW,
      'override_active': instance.overrideActive,
      'override_current_a': instance.overrideCurrentA,
      'override_until': instance.overrideUntil,
      'session_kwh': instance.sessionKwh,
      'guard_enabled': instance.guardEnabled,
      'guard_active': instance.guardActive,
      'guard_factor': instance.guardFactor,
      'guard_linear_mode': instance.guardLinearMode,
      'guard_required_soc': instance.guardRequiredSoc,
      'guard_sunset': instance.guardSunset,
      'guard_sunrise': instance.guardSunrise,
      'guard_reason': instance.guardReason,
      'guard_cloud_pct': instance.guardCloudPct,
      'guard_tomorrow_cloud_pct': instance.guardTomorrowCloudPct,
      'guard_tomorrow_boost': instance.guardTomorrowBoost,
      'guard_seasonal_extra': instance.guardSeasonalExtra,
    };
