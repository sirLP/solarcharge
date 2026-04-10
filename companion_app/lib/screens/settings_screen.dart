import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/api_providers.dart';

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  late final TextEditingController _urlController;
  bool _saved = false;

  @override
  void initState() {
    super.initState();
    final current = ref.read(baseUrlProvider);
    _urlController = TextEditingController(text: current);
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final url = _urlController.text.trim();
    if (url.isEmpty) return;
    await ref.read(baseUrlProvider.notifier).update(url);
    // Force fresh status load with new URL.
    ref.read(statusProvider.notifier).refresh();
    setState(() => _saved = true);
    Future.delayed(const Duration(seconds: 2),
        () => setState(() => _saved = false));
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      navigationBar: const CupertinoNavigationBar(
        middle: Text('Settings'),
      ),
      child: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            const _SectionLabel('Server'),
            _Card(
              children: [
                const Text(
                  'Server URL',
                  style: TextStyle(
                    fontWeight: FontWeight.w600,
                    fontSize: 14,
                    color: CupertinoColors.systemGrey,
                  ),
                ),
                const SizedBox(height: 6),
                CupertinoTextField(
                  controller: _urlController,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  placeholder: 'http://solarcharge.local',
                  padding:
                      const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  decoration: BoxDecoration(
                    color: CupertinoColors.tertiarySystemGroupedBackground,
                    borderRadius: BorderRadius.circular(8),
                  ),
                ),
                const SizedBox(height: 4),
                const Text(
                  'The hostname or IP address of your SolarCharge server.',
                  style: TextStyle(
                    fontSize: 12,
                    color: CupertinoColors.systemGrey,
                  ),
                ),
                const SizedBox(height: 16),
                CupertinoButton.filled(
                  onPressed: _save,
                  child: Text(_saved ? '✓ Saved' : 'Save'),
                ),
              ],
            ),
            const SizedBox(height: 24),
            const _AboutSection(),
          ],
        ),
      ),
    );
  }
}

class _AboutSection extends StatelessWidget {
  const _AboutSection();

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SectionLabel('About'),
        _Card(
          children: [
            const Text(
              'SolarCharge Companion',
              style: TextStyle(fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 4),
            const Text(
              'Monitor and control your SolarCharge EV wallbox.',
              style: TextStyle(
                color: CupertinoColors.systemGrey,
                fontSize: 13,
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Text(
        text.toUpperCase(),
        style: const TextStyle(
          fontWeight: FontWeight.w600,
          fontSize: 12,
          color: CupertinoColors.systemGrey,
          letterSpacing: 0.8,
        ),
      ),
    );
  }
}

class _Card extends StatelessWidget {
  const _Card({required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: CupertinoColors.secondarySystemGroupedBackground,
        borderRadius: BorderRadius.circular(14),
      ),
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    );
  }
}
