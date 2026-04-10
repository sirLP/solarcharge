// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'config.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

ChargeConfig _$ChargeConfigFromJson(Map<String, dynamic> json) => ChargeConfig(
  pollIntervalS: (json['poll_interval_s'] as num).toInt(),
  startThresholdA: (json['start_threshold_a'] as num).toDouble(),
  stopThresholdA: (json['stop_threshold_a'] as num).toDouble(),
  rampStepA: (json['ramp_step_a'] as num).toDouble(),
  minCurrentA: (json['min_current_a'] as num).toDouble(),
  maxCurrentA: (json['max_current_a'] as num).toDouble(),
  phases: (json['phases'] as num).toInt(),
  voltagePerPhase: (json['voltage_per_phase'] as num).toDouble(),
);

Map<String, dynamic> _$ChargeConfigToJson(ChargeConfig instance) =>
    <String, dynamic>{
      'poll_interval_s': instance.pollIntervalS,
      'start_threshold_a': instance.startThresholdA,
      'stop_threshold_a': instance.stopThresholdA,
      'ramp_step_a': instance.rampStepA,
      'min_current_a': instance.minCurrentA,
      'max_current_a': instance.maxCurrentA,
      'phases': instance.phases,
      'voltage_per_phase': instance.voltagePerPhase,
    };
