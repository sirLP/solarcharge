/// Low-level HTTP client for the SolarCharge backend.
///
/// All methods throw [ApiException] on non-2xx responses.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/config.dart';
import '../models/status.dart';

class ApiException implements Exception {
  final int statusCode;
  final String message;
  const ApiException(this.statusCode, this.message);

  @override
  String toString() => 'ApiException($statusCode): $message';
}

class ApiService {
  ApiService({required String baseUrl})
      : _base = Uri.parse(baseUrl.endsWith('/')
            ? baseUrl.substring(0, baseUrl.length - 1)
            : baseUrl);

  final Uri _base;

  // ── helpers ─────────────────────────────────────────────────────────────

  Uri _uri(String path) => _base.replace(path: path);

  Map<String, dynamic> _decode(http.Response resp) {
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw ApiException(resp.statusCode, resp.body);
    }
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  // ── status ───────────────────────────────────────────────────────────────

  Future<ChargeStatus> fetchStatus() async {
    final resp = await http.get(_uri('/api/status'));
    return ChargeStatus.fromJson(_decode(resp));
  }

  // ── config ───────────────────────────────────────────────────────────────

  Future<ChargeConfig> fetchConfig() async {
    final resp = await http.get(_uri('/api/config'));
    return ChargeConfig.fromJson(_decode(resp));
  }

  Future<void> updateConfig(Map<String, dynamic> patch) async {
    final resp = await http.post(
      _uri('/api/config'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode(patch),
    );
    _decode(resp);
  }

  // ── override ─────────────────────────────────────────────────────────────

  /// Set a manual current override.
  ///
  /// [currentA] – target current in amps (6 – 32).
  /// [durationMinutes] – optional duration; null means indefinite.
  Future<void> setOverride({
    required double currentA,
    int? durationMinutes,
  }) async {
    final body = <String, dynamic>{
      'action': 'set_current',
      'current_a': currentA,
      // ignore: use_null_aware_elements
      if (durationMinutes != null) 'duration_minutes': durationMinutes,
    };
    final resp = await http.post(
      _uri('/api/override'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode(body),
    );
    _decode(resp);
  }

  /// Resume automatic charging mode.
  Future<void> resumeAuto() async {
    final resp = await http.post(
      _uri('/api/override'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'action': 'resume'}),
    );
    _decode(resp);
  }

  // ── diagnostics ──────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> fetchDiagnostics() async {
    final resp = await http.get(_uri('/api/diagnostics'));
    return _decode(resp);
  }

  // ── RFID guard ───────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> fetchRfidConfig() async {
    final resp = await http.get(_uri('/api/rfid'));
    return _decode(resp);
  }

  Future<List<Map<String, dynamic>>> fetchRfidBlocked() async {
    final resp = await http.get(_uri('/api/rfid/blocked'));
    final body = _decode(resp);
    final blocked = body['blocked'];
    if (blocked is List) {
      return blocked.cast<Map<String, dynamic>>();
    }
    return const [];
  }

  // ── history ──────────────────────────────────────────────────────────────

  Future<List<Map<String, dynamic>>> fetchHistory({int days = 7}) async {
    final resp = await http.get(
      _uri('/api/history').replace(queryParameters: {'days': '$days'}),
    );
    final body = jsonDecode(resp.body);
    return (body as List).cast<Map<String, dynamic>>();
  }

  // ── timeseries ───────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> fetchTimeseries({int hours = 24}) async {
    final resp = await http.get(
      _uri('/api/timeseries')
          .replace(queryParameters: {'hours': '$hours'}),
    );
    return _decode(resp);
  }
}
