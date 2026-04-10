import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/config.dart';
import '../providers/api_providers.dart';

const String _kAppVersion = '1.0.0+1';

class DetailsScreen extends ConsumerStatefulWidget {
  const DetailsScreen({super.key});

  @override
  ConsumerState<DetailsScreen> createState() => _DetailsScreenState();
}

class _DetailsScreenState extends ConsumerState<DetailsScreen> {
  late Future<_DetailsData> _future;
  late final TextEditingController _urlController;
  bool _urlSaved = false;

  @override
  void initState() {
    super.initState();
    _urlController = TextEditingController(text: ref.read(baseUrlProvider));
    _future = _load();
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  Future<_DetailsData> _load() async {
    final api = ref.read(apiServiceProvider);
    final config = await api.fetchConfig();
    final diagnostics = await api.fetchDiagnostics();
    final rfid = await api.fetchRfidConfig();
    final blocked = await api.fetchRfidBlocked();
    return _DetailsData(
      config: config,
      diagnostics: diagnostics,
      rfid: rfid,
      blocked: blocked,
    );
  }

  Future<void> _refresh() async {
    setState(() {
      _future = _load();
    });
  }

  Future<void> _saveServerUrl() async {
    final url = _urlController.text.trim();
    if (url.isEmpty) return;
    await ref.read(baseUrlProvider.notifier).update(url);
    // Force status poll to switch to the new backend immediately.
    ref.read(statusProvider.notifier).refresh();
    setState(() {
      _urlSaved = true;
      _future = _load();
    });
    Future.delayed(const Duration(seconds: 2), () {
      if (!mounted) return;
      setState(() => _urlSaved = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      navigationBar: CupertinoNavigationBar(
        middle: const Text('Details'),
        trailing: CupertinoButton(
          padding: EdgeInsets.zero,
          onPressed: _refresh,
          child: const Icon(CupertinoIcons.refresh),
        ),
      ),
      child: SafeArea(
        child: FutureBuilder<_DetailsData>(
          future: _future,
          builder: (context, snap) {
            if (snap.connectionState == ConnectionState.waiting) {
              return const Center(child: CupertinoActivityIndicator());
            }
            if (snap.hasError) {
              return Center(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text('Could not load details: ${snap.error}'),
                ),
              );
            }
            final data = snap.data;
            if (data == null) {
              return const Center(child: Text('No details available.'));
            }
            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                _Card(
                  title: 'Server',
                  children: [
                    const Text(
                      'SolarCharge server URL',
                      style: TextStyle(
                        color: CupertinoColors.systemGrey,
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(height: 8),
                    CupertinoTextField(
                      controller: _urlController,
                      keyboardType: TextInputType.url,
                      autocorrect: false,
                      placeholder: 'http://solarcharge.local',
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                      decoration: BoxDecoration(
                        color: CupertinoColors.systemBackground,
                        borderRadius: BorderRadius.circular(8),
                      ),
                    ),
                    const SizedBox(height: 8),
                    CupertinoButton.filled(
                      onPressed: _saveServerUrl,
                      child: Text(_urlSaved ? 'Saved' : 'Save URL'),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                _Card(
                  title: 'Diagnostics',
                  children: [
                    _Row('SENEC URL', _asText(data.diagnostics['senec']?['url'])),
                    _Row('SENEC timestamp', _asText(data.diagnostics['senec']?['timestamp'])),
                    _Row('Alfen host', _asText(data.diagnostics['alfen']?['host'])),
                    _Row('Alfen timestamp', _asText(data.diagnostics['alfen']?['timestamp'])),
                    _Row(
                      'Alfen reads',
                      _countText(data.diagnostics['alfen']?['reads']),
                    ),
                    _Row(
                      'Alfen writes',
                      _countText(data.diagnostics['alfen']?['writes']),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                _Card(
                  title: 'Config',
                  children: [
                    _Row('Poll interval', '${data.config.pollIntervalS} s'),
                    _Row('Start threshold', '${data.config.startThresholdA.toStringAsFixed(1)} A'),
                    _Row('Stop threshold', '${data.config.stopThresholdA.toStringAsFixed(1)} A'),
                    _Row('Ramp step', '${data.config.rampStepA.toStringAsFixed(1)} A'),
                    _Row('Min current', '${data.config.minCurrentA.toStringAsFixed(1)} A'),
                    _Row('Max current', '${data.config.maxCurrentA.toStringAsFixed(1)} A'),
                    _Row('Phases', '${data.config.phases}'),
                    _Row('Voltage / phase', '${data.config.voltagePerPhase.toStringAsFixed(0)} V'),
                  ],
                ),
                const SizedBox(height: 12),
                _Card(
                  title: 'RFID Card Guard',
                  children: [
                    _Row('Enabled', (data.rfid['enabled'] == true) ? 'Yes' : 'No'),
                    _Row('Allowed cards', _countText(data.rfid['cards'])),
                    const SizedBox(height: 8),
                    const Text(
                      'Blocked attempts',
                      style: TextStyle(
                        color: CupertinoColors.systemGrey,
                        fontSize: 13,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 6),
                    if (data.blocked.isEmpty)
                      const Text(
                        'No blocked attempts.',
                        style: TextStyle(color: CupertinoColors.systemGrey),
                      )
                    else
                      ...data.blocked.take(10).map(
                        (e) => Padding(
                          padding: const EdgeInsets.symmetric(vertical: 2),
                          child: Text(
                            '${_asText(e['ts'])}  ·  UID ${_asText(e['uid'])}  ·  ${_asText(e['name'])}',
                            style: const TextStyle(fontSize: 13),
                          ),
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 20),
                Center(
                  child: Text(
                    'App version $_kAppVersion',
                    style: const TextStyle(
                      color: CupertinoColors.systemGrey,
                      fontSize: 12,
                    ),
                  ),
                ),
                const SizedBox(height: 8),
              ],
            );
          },
        ),
      ),
    );
  }

  String _asText(Object? v) {
    if (v == null) return '—';
    final s = v.toString().trim();
    return s.isEmpty ? '—' : s;
  }

  String _countText(Object? v) {
    if (v is List) return '${v.length}';
    return '0';
  }
}

class _DetailsData {
  const _DetailsData({
    required this.config,
    required this.diagnostics,
    required this.rfid,
    required this.blocked,
  });

  final ChargeConfig config;
  final Map<String, dynamic> diagnostics;
  final Map<String, dynamic> rfid;
  final List<Map<String, dynamic>> blocked;
}

class _Card extends StatelessWidget {
  const _Card({required this.title, required this.children});

  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: CupertinoColors.secondarySystemGroupedBackground,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(
              fontWeight: FontWeight.w700,
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
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              color: CupertinoColors.systemGrey,
              fontSize: 12,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            value,
            style: const TextStyle(
              fontWeight: FontWeight.w600,
              fontSize: 14,
            ),
          ),
        ],
      ),
    );
  }
}
