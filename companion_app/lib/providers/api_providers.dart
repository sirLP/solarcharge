/// Riverpod providers for the SolarCharge backend.
///
/// The [apiServiceProvider] is the single source of truth for the [ApiService]
/// instance.  All data providers delegate to it.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../models/config.dart';
import '../models/status.dart';
import '../services/api_service.dart';

// ── settings ─────────────────────────────────────────────────────────────────

/// Provider for [SharedPreferences]; must be overridden in main() after await.
final sharedPreferencesProvider = Provider<SharedPreferences>(
  (_) => throw UnimplementedError('SharedPreferences not initialised'),
);

/// The base URL entered in the Settings screen.
final baseUrlProvider = StateNotifierProvider<BaseUrlNotifier, String>((ref) {
  final prefs = ref.watch(sharedPreferencesProvider);
  return BaseUrlNotifier(prefs);
});

class BaseUrlNotifier extends StateNotifier<String> {
  BaseUrlNotifier(this._prefs)
      : super(_prefs.getString(_key) ?? '');

  static const _key = 'base_url';
  final SharedPreferences _prefs;

  Future<void> update(String url) async {
    state = url;
    await _prefs.setString(_key, url);
  }
}

// ── API service ───────────────────────────────────────────────────────────────

final apiServiceProvider = Provider<ApiService>((ref) {
  final baseUrl = ref.watch(baseUrlProvider);
  return ApiService(baseUrl: baseUrl);
});

// ── status ────────────────────────────────────────────────────────────────────

/// Auto-refreshes every [_pollInterval].
final statusProvider =
    AsyncNotifierProvider<StatusNotifier, ChargeStatus>(StatusNotifier.new);

const _pollInterval = Duration(seconds: 10);

class StatusNotifier extends AsyncNotifier<ChargeStatus> {
  @override
  Future<ChargeStatus> build() async {
    final timer = Timer(_pollInterval, ref.invalidateSelf);
    ref.onDispose(timer.cancel);
    return ref.watch(apiServiceProvider).fetchStatus();
  }

  /// Force an immediate refresh.
  Future<void> refresh() async {
    ref.invalidateSelf();
  }
}

// ── config ────────────────────────────────────────────────────────────────────

final configProvider =
    AsyncNotifierProvider<ConfigNotifier, ChargeConfig>(ConfigNotifier.new);

class ConfigNotifier extends AsyncNotifier<ChargeConfig> {
  @override
  Future<ChargeConfig> build() =>
      ref.watch(apiServiceProvider).fetchConfig();

  Future<void> patchConfig(Map<String, dynamic> patch) async {
    await ref.watch(apiServiceProvider).updateConfig(patch);
    ref.invalidateSelf();
  }
}
