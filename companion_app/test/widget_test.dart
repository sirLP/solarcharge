import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:solar_charge_companion/app.dart';
import 'package:solar_charge_companion/providers/api_providers.dart';

void main() {
  testWidgets('app renders dashboard tab', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({
      'base_url': 'http://solarcharge.local',
    });
    final prefs = await SharedPreferences.getInstance();

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sharedPreferencesProvider.overrideWithValue(prefs),
        ],
        child: const SolarChargeApp(),
      ),
    );
    await tester.pump();

    expect(find.text('Dashboard'), findsOneWidget);
  });
}
