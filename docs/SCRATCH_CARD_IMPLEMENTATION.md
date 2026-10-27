# Scratch Card — Flutter Implementation Guide

Depends on the wallet already being wired up (`WALLET_IMPLEMENTATION.md`) —
scratching a card credits the same wallet, and this guide reuses its
`ApiException`/`mapDioError` helper and assumes the same authenticated `Dio`
instance and `provider` state-management choice. Build the wallet first.

**Important backend behavior**: the reward amount is decided the moment a card
is *earned* (issuance time), not when the user scratches it — same as a real
physical scratch card already has its prize printed underneath before anyone
scratches the foil off. That means `rewardRupees` is already present on every
`ScratchCard` object you get back — from the post-creation response, from the
list endpoint, from anywhere — even while `status` is still `unscratched`.
Practically: **you never need to wait on the network to know or display the
reward amount.** The `scratch` API call's only job is to actually commit that
already-known amount into the wallet; it does no computation, so it's fast and
you don't need a loading spinner blocking the reveal animation itself (only,
optionally, a spinner on the "committing to wallet" part if you want to be
extra safe about slow connections — the number on screen never needs to wait
for it).

## Package additions

Same as wallet (`dio`, `provider`, `uuid`) — no new dependencies for the API/state
layer. The reveal animation below uses only core Flutter (`AnimatedSwitcher` +
`GestureDetector`); swap in a dedicated scratch-off package later if you want the
literal "scratch with your finger" gesture instead of tap-to-reveal.

---

## 1. Data model

`lib/models/scratch_card.dart`

```dart
enum ScratchCardStatus { unscratched, scratched, expired }

ScratchCardStatus _parseStatus(String s) => ScratchCardStatus.values.firstWhere(
      (v) => v.name == s,
      orElse: () => ScratchCardStatus.expired,
    );

class ScratchCard {
  final String id;
  final ScratchCardStatus status;
  final DateTime earnedAt;
  final DateTime expiresAt;
  final DateTime? scratchedAt;
  final String? rewardRupees; // already populated even when unscratched — see note below

  ScratchCard({
    required this.id,
    required this.status,
    required this.earnedAt,
    required this.expiresAt,
    this.scratchedAt,
    this.rewardRupees,
  });

  factory ScratchCard.fromJson(Map<String, dynamic> json) => ScratchCard(
        id: json['id'] as String,
        status: _parseStatus(json['status'] as String),
        earnedAt: DateTime.parse(json['earnedAt'] as String),
        expiresAt: DateTime.parse(json['expiresAt'] as String),
        scratchedAt: json['scratchedAt'] != null ? DateTime.parse(json['scratchedAt'] as String) : null,
        rewardRupees: json['rewardRupees'] as String?,
      );

  Duration get timeUntilExpiry => expiresAt.difference(DateTime.now());
}

class ScratchResult {
  final String cardId;
  final int rewardPaise;
  final String rewardRupees;
  final String walletAvailableBalance;

  ScratchResult({
    required this.cardId,
    required this.rewardPaise,
    required this.rewardRupees,
    required this.walletAvailableBalance,
  });

  factory ScratchResult.fromJson(Map<String, dynamic> json) => ScratchResult(
        cardId: json['cardId'] as String,
        rewardPaise: json['rewardPaise'] as int,
        rewardRupees: json['rewardRupees'] as String,
        walletAvailableBalance: json['walletAvailableBalance'] as String,
      );
}
```

## 2. API service

`lib/api/scratch_card_api.dart`

```dart
import 'package:dio/dio.dart';
import '../models/scratch_card.dart';
import 'api_exception.dart'; // shared with wallet_api.dart

class ScratchCardApi {
  final Dio _dio;
  ScratchCardApi(this._dio);

  Future<List<ScratchCard>> listCards(String userId) async {
    try {
      final res = await _dio.get('/api/scratch-cards/$userId');
      final list = res.data['scratchCards'] as List;
      return list.map((j) => ScratchCard.fromJson(j)).toList();
    } on DioException catch (e) {
      throw mapDioError(e);
    }
  }

  /// [idempotencyKey]: one fresh uuid.v4() per scratch attempt, generated
  /// right before this call — reuse the same key if you retry after a
  /// timeout, so a retry replays the same result instead of erroring 409.
  Future<ScratchResult> scratch(String userId, String cardId, {required String idempotencyKey}) async {
    try {
      final res = await _dio.post('/api/scratch-cards/$userId/$cardId/scratch', data: {
        'idempotencyKey': idempotencyKey,
      });
      return ScratchResult.fromJson(res.data);
    } on DioException catch (e) {
      throw mapDioError(e);
    }
  }
}
```

## 3. State (Provider)

`lib/providers/scratch_card_provider.dart`

```dart
import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';
import '../api/scratch_card_api.dart';
import '../api/api_exception.dart';
import '../models/scratch_card.dart';

enum ScratchCardLoadState { idle, loading, loaded, error }

class ScratchCardProvider extends ChangeNotifier {
  final ScratchCardApi _api;
  final String userId;
  ScratchCardProvider(this._api, this.userId);

  ScratchCardLoadState state = ScratchCardLoadState.idle;
  List<ScratchCard> cards = [];
  String? errorMessage;

  // Per-card in-flight/error tracking, keyed by card id, so scratching one
  // card doesn't disable the whole list.
  final Set<String> _scratchingIds = {};
  final Map<String, String> _scratchErrors = {};

  bool isScratching(String cardId) => _scratchingIds.contains(cardId);
  String? scratchError(String cardId) => _scratchErrors[cardId];

  List<ScratchCard> get unscratched =>
      cards.where((c) => c.status == ScratchCardStatus.unscratched).toList();

  Future<void> load() async {
    state = ScratchCardLoadState.loading;
    notifyListeners();
    try {
      cards = await _api.listCards(userId);
      state = ScratchCardLoadState.loaded;
    } catch (e) {
      errorMessage = e is ApiException ? e.detail : 'Failed to load scratch cards';
      state = ScratchCardLoadState.error;
    }
    notifyListeners();
  }

  /// Returns the reveal result on success, null on failure (with
  /// [scratchError] set for that card id). On 409/410 the local card list is
  /// refreshed so the UI reflects the true server state instead of a stale
  /// "unscratched" card.
  Future<ScratchResult?> scratchCard(String cardId) async {
    _scratchingIds.add(cardId);
    _scratchErrors.remove(cardId);
    notifyListeners();

    final idempotencyKey = const Uuid().v4();
    try {
      final result = await _api.scratch(userId, cardId, idempotencyKey: idempotencyKey);
      // Reflect the reveal locally instead of a full reload — keeps the
      // reveal animation snappy; a background refresh can still happen after.
      final idx = cards.indexWhere((c) => c.id == cardId);
      if (idx != -1) {
        cards[idx] = ScratchCard(
          id: cards[idx].id,
          status: ScratchCardStatus.scratched,
          earnedAt: cards[idx].earnedAt,
          expiresAt: cards[idx].expiresAt,
          scratchedAt: DateTime.now(),
          rewardRupees: result.rewardRupees,
        );
      }
      return result;
    } catch (e) {
      final apiError = e is ApiException ? e : null;
      if (apiError?.statusCode == 409 || apiError?.statusCode == 410) {
        await load(); // card state changed server-side (already scratched / expired) — resync
      }
      _scratchErrors[cardId] = _scratchErrorMessage(apiError);
      return null;
    } finally {
      _scratchingIds.remove(cardId);
      notifyListeners();
    }
  }

  String _scratchErrorMessage(ApiException? e) {
    switch (e?.statusCode) {
      case 409:
        return 'This card was already scratched.';
      case 410:
        return 'This card has expired.';
      case 404:
        return 'Card not found.';
      default:
        return 'Something went wrong. Please try again.';
    }
  }
}
```

**Important**: after a successful `scratchCard()`, also refresh your `WalletProvider`
(e.g. `context.read<WalletProvider>().load()`, or just splice in
`result.walletAvailableBalance` directly if `WalletProvider` exposes a setter) so
the wallet balance shown elsewhere in the app updates immediately.

## 4. UI

### Post-creation banner

Wherever the "create post" API call currently lives, read the new field off the
response and show a banner/snackbar:

```dart
final response = await postsApi.createPost(userId, postData);
if (response['scratchCardEarned'] == true) {
  // e.g. a SnackBar, or navigate straight into the scratch-card screen
  ScaffoldMessenger.of(context).showSnackBar(
    const SnackBar(content: Text('🎉 You earned a scratch card! Check your rewards.')),
  );
  context.read<ScratchCardProvider>().load(); // so the list screen is fresh when they open it
}
```

### Scratch card list + reveal

`lib/screens/scratch_cards_screen.dart`

```dart
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../models/scratch_card.dart';
import '../providers/scratch_card_provider.dart';
import '../providers/wallet_provider.dart';
import 'reward_reveal_dialog.dart';

class ScratchCardsScreen extends StatelessWidget {
  const ScratchCardsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Scratch Cards')),
      body: Consumer<ScratchCardProvider>(
        builder: (context, provider, _) {
          if (provider.state == ScratchCardLoadState.loading || provider.state == ScratchCardLoadState.idle) {
            return const Center(child: CircularProgressIndicator());
          }
          if (provider.state == ScratchCardLoadState.error) {
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(provider.errorMessage ?? 'Failed to load'),
                  const SizedBox(height: 12),
                  ElevatedButton(onPressed: provider.load, child: const Text('Retry')),
                ],
              ),
            );
          }

          if (provider.cards.isEmpty) {
            return const Center(child: Text('No scratch cards yet — post a truck to earn one!'));
          }

          return RefreshIndicator(
            onRefresh: provider.load,
            child: GridView.builder(
              padding: const EdgeInsets.all(16),
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 2, mainAxisSpacing: 16, crossAxisSpacing: 16, childAspectRatio: 0.9,
              ),
              itemCount: provider.cards.length,
              itemBuilder: (context, i) => _ScratchCardTile(card: provider.cards[i]),
            ),
          );
        },
      ),
    );
  }
}

class _ScratchCardTile extends StatelessWidget {
  final ScratchCard card;
  const _ScratchCardTile({required this.card});

  @override
  Widget build(BuildContext context) {
    return Consumer<ScratchCardProvider>(
      builder: (context, provider, _) {
        final isScratching = provider.isScratching(card.id);
        final error = provider.scratchError(card.id);

        return GestureDetector(
          // KEY POINT: card.rewardRupees is already known — it came back
          // populated on this exact card object from the list/post-creation
          // response, computed server-side at issuance time, not reveal time.
          // So the reveal dialog shows INSTANTLY off local data; the network
          // call only needs to happen to actually commit the money into the
          // wallet, and doesn't have to block the animation the user sees.
          onTap: card.status != ScratchCardStatus.unscratched || isScratching
              ? null
              : () async {
                  await showRewardRevealDialog(context, card.rewardRupees!);
                  if (!context.mounted) return;
                  // Commit in the background — no spinner blocking the reveal
                  // the user already saw. scratchCard() still marks the tile
                  // "scratching" internally so a fast double-tap is guarded,
                  // but the UI here doesn't wait on it before celebrating.
                  final result = await provider.scratchCard(card.id);
                  if (!context.mounted) return;
                  if (result != null) {
                    context.read<WalletProvider>().load(); // sync the balance now that it's committed
                  } else if (error != null) {
                    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error)));
                  }
                },
          child: Card(
            color: switch (card.status) {
              ScratchCardStatus.unscratched => Colors.amber.shade100,
              ScratchCardStatus.scratched => Colors.green.shade50,
              ScratchCardStatus.expired => Colors.grey.shade200,
            },
            child: Center(
              child: isScratching
                  ? const CircularProgressIndicator()
                  : Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(
                          switch (card.status) {
                            ScratchCardStatus.unscratched => Icons.card_giftcard,
                            ScratchCardStatus.scratched => Icons.check_circle,
                            ScratchCardStatus.expired => Icons.timer_off,
                          },
                          size: 40,
                        ),
                        const SizedBox(height: 8),
                        Text(switch (card.status) {
                          ScratchCardStatus.unscratched => 'Tap to reveal',
                          ScratchCardStatus.scratched => '₹${card.rewardRupees}',
                          ScratchCardStatus.expired => 'Expired',
                        }),
                      ],
                    ),
            ),
          ),
        );
      },
    );
  }
}
```

### Reward reveal dialog

`lib/screens/reward_reveal_dialog.dart`

```dart
import 'package:flutter/material.dart';

Future<void> showRewardRevealDialog(BuildContext context, String rewardRupees) {
  return showDialog(
    context: context,
    builder: (_) => AlertDialog(
      title: const Text('🎉 You won!'),
      content: Text(
        '₹$rewardRupees',
        textAlign: TextAlign.center,
        style: const TextStyle(fontSize: 36, fontWeight: FontWeight.bold, color: Colors.green),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Nice!')),
      ],
    ),
  );
}
```

Swap this dialog for a real scratch-off gesture / confetti animation later — the
provider layer doesn't change either way, since the reward is already known
before you'd even start the reveal animation (it came from the card object
itself, not from an API call). The commit-to-wallet API call now happens
*after* or *alongside* the animation, not before it — there's nothing left to
wait on to know what number to show. If you want the animation to play
`scratchCard()` on tap but don't render the result until the scratch gesture/
animation completes, then show whatever `ScratchResult` already came back.

## 5. Testing checklist before shipping

- [ ] `scratchCardEarned: true` on post creation triggers the banner; `false`
      shows nothing (don't assume `false` is an error)
- [ ] Unscratched card is tappable; scratched/expired cards are not
- [ ] First-ever scratch for a brand-new user shows exactly ₹50 (fixed bonus) —
      don't hardcode this number as "typical" anywhere else in copy, since every
      later scratch is a random amount within a declining ceiling
- [ ] Tapping a card mid-request disables that tile only, not the whole grid
- [ ] Double-tapping the same card fast (before the first request returns)
      does not fire two scratch requests — the per-card `isScratching` guard
      should prevent this, but explicitly test it
- [ ] Scratching an already-scratched card (e.g. stale list from another
      device) shows "already scratched" and refreshes to the true state,
      no crash
- [ ] Scratching an expired card shows "expired" and the tile updates
      to the expired visual state without a manual pull-to-refresh
- [ ] After a successful scratch, the Wallet screen's balance updates without
      the user having to manually navigate there and back
- [ ] Countdown/expiry display (`timeUntilExpiry`) reads sensibly for a card
      earned today (~29 days left) vs one about to expire
