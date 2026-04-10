// Config model matching the backend's /api/config response.
// Run `dart run build_runner build` to regenerate config.g.dart.

import 'package:json_annotation/json_annotation.dart';

part 'config.g.dart';

@JsonSerializable()
class ChargeConfig {
  @JsonKey(name: 'poll_interval_s')
  final int pollIntervalS;
  @JsonKey(name: 'start_threshold_a')
  final double startThresholdA;
  @JsonKey(name: 'stop_threshold_a')
  final double stopThresholdA;
  @JsonKey(name: 'ramp_step_a')
  final double rampStepA;
  @JsonKey(name: 'min_current_a')
  final double minCurrentA;
  @JsonKey(name: 'max_current_a')
  final double maxCurrentA;
  final int phases;
  @JsonKey(name: 'voltage_per_phase')
  final double voltagePerPhase;

  const ChargeConfig({
    required this.pollIntervalS,
    required this.startThresholdA,
    required this.stopThresholdA,
    required this.rampStepA,
    required this.minCurrentA,
    required this.maxCurrentA,
    required this.phases,
    required this.voltagePerPhase,
  });

  factory ChargeConfig.fromJson(Map<String, dynamic> json) =>
      _$ChargeConfigFromJson(json);

  Map<String, dynamic> toJson() => _$ChargeConfigToJson(this);
}
