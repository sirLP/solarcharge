import 'package:flutter/cupertino.dart';

import 'screens/dashboard_screen.dart';
import 'screens/details_screen.dart';
import 'screens/settings_screen.dart';

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

class _AppTabs extends StatefulWidget {
  const _AppTabs();

  @override
  State<_AppTabs> createState() => _AppTabsState();
}

class _AppTabsState extends State<_AppTabs> {
  int _currentIndex = 0;
  late final PageController _pageController;

  static const _pages = [
    DashboardScreen(),
    DetailsScreen(),
    SettingsScreen(),
  ];

  @override
  void initState() {
    super.initState();
    _pageController = PageController();
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  void _onTabTapped(int index) {
    _pageController.animateToPage(
      index,
      duration: const Duration(milliseconds: 300),
      curve: Curves.easeInOut,
    );
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      child: Column(
        children: [
          Expanded(
            child: PageView(
              controller: _pageController,
              onPageChanged: (index) {
                setState(() => _currentIndex = index);
              },
              children: _pages,
            ),
          ),
          CupertinoTabBar(
            currentIndex: _currentIndex,
            onTap: _onTabTapped,
            items: const [
              BottomNavigationBarItem(
                icon: Icon(CupertinoIcons.bolt_fill),
                label: 'Dashboard',
              ),
              BottomNavigationBarItem(
                icon: Icon(CupertinoIcons.doc_text_search),
                label: 'Details',
              ),
              BottomNavigationBarItem(
                icon: Icon(CupertinoIcons.settings),
                label: 'Settings',
              ),
            ],
          ),
        ],
      ),
    );
  }
}
