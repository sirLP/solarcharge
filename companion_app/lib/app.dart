import 'package:flutter/cupertino.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'screens/dashboard_screen.dart';
import 'screens/details_screen.dart';

class SolarChargeApp extends StatelessWidget {
  const SolarChargeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return const CupertinoApp(
      title: 'SolarCharge',
      theme: CupertinoThemeData(
        primaryColor: CupertinoColors.systemGreen,
        brightness: Brightness.light,
      ),
      home: _AppTabs(),
    );
  }
}

class _AppTabs extends ConsumerWidget {
  const _AppTabs();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return CupertinoTabScaffold(
      tabBar: CupertinoTabBar(
        items: const [
          BottomNavigationBarItem(
            icon: Icon(CupertinoIcons.bolt_fill),
            label: 'Dashboard',
          ),
          BottomNavigationBarItem(
            icon: Icon(CupertinoIcons.doc_text_search),
            label: 'Details',
          ),
        ],
      ),
      tabBuilder: (context, index) {
        return switch (index) {
          0 => CupertinoTabView(
              builder: (_) => const DashboardScreen(),
            ),
          _ => CupertinoTabView(
              builder: (_) => const DetailsScreen(),
            ),
        };
      },
    );
  }
}
