# Wallet — Flutter Implementation Guide

Implements the wallet screen (balance, transaction history, redeem-to-UPI) against
the backend endpoints documented in `WALLET_SCRATCH_CARD_INTEGRATION.md`. Scratch
cards are a separate follow-up (`SCRATCH_CARD_IMPLEMENTATION.md`) — this file only
covers the wallet.

## Assumptions (adjust to match your actual app)

- HTTP client: `dio` (swap for `http` if that's what the app already uses — the
  API-call bodies below translate directly).
- State management: a plain `ChangeNotifier` + `provider` — swap for Riverpod/Bloc/
  GetX if the app already standardized on one of those; only `WalletProvider`'s
  internals change, not the API service or models.
- You already have an authenticated `Dio` instance (or equivalent) that attaches
  `Authorization: Bearer <accessToken>` to every request, and a way to read the
  current logged-in user's id. Plug into those instead of the placeholders below.
- Package additions needed: `uuid` (idempotency keys), `intl` (currency formatting).

```yaml
dependencies:
  dio: ^5.4.0
  provider: ^6.1.0
  uuid: ^4.3.0
  intl: ^0.19.0
```

---

## 1. Data models

`lib/models/wallet.dart`

```dart
class Wallet {
  final String availableBalance; // "150.00" — keep as string, only parse for display math
  final String reservedBalance;
  final String status; // "active" | "frozen"

  Wallet({required this.availableBalance, required this.reservedBalance, required this.status});

  factory Wallet.fromJson(Map<String, dynamic> json) => Wallet(
        availableBalance: json['availableBalance'] as String,
        reservedBalance: json['reservedBalance'] as String,
        status: json['status'] as String,
      );

  bool get isFrozen => status == 'frozen';
}

class WalletTransaction {
  final String id;
  final String type; // credit | reserve_redeem | confirm_payout | release_reserve
  final String amount;
  final String availableAfter;
  final String reservedAfter;
  final String referenceType; // "scratch_card" | "redeem_request"
  final String? referenceId;
  final String? note;
  final DateTime createdAt;

  WalletTransaction({
    required this.id,
    required this.type,
    required this.amount,
    required this.availableAfter,
    required this.reservedAfter,
    required this.referenceType,
    this.referenceId,
    this.note,
    required this.createdAt,
  });

  factory WalletTransaction.fromJson(Map<String, dynamic> json) => WalletTransaction(
        id: json['id'] as String,
        type: json['type'] as String,
        amount: json['amount'] as String,
        availableAfter: json['availableAfter'] as String,
        reservedAfter: json['reservedAfter'] as String,
        referenceType: json['referenceType'] as String,
        referenceId: json['referenceId'] as String?,
        note: json['note'] as String?,
        createdAt: DateTime.parse(json['createdAt'] as String),
      );

  /// Positive/negative sign for display — credits and released reserves add
  /// to the user's money, redeems and confirmed payouts move it out of reach.
  bool get isCredit => type == 'credit' || type == 'release_reserve';
}

class RedeemRequest {
  final String id;
  final String amount;
  final String upiId;
  final String status; // pending | paid | rejected
  final DateTime createdAt;

  RedeemRequest({
    required this.id,
    required this.amount,
    required this.upiId,
    required this.status,
    required this.createdAt,
  });

  factory RedeemRequest.fromJson(Map<String, dynamic> json) => RedeemRequest(
        id: json['id'] as String,
        amount: json['amount'] as String,
        upiId: json['upiId'] as String,
        status: json['status'] as String,
        createdAt: DateTime.parse(json['createdAt'] as String),
      );
}
```

## 2. API exceptions

`lib/api/api_exception.dart` (shared across wallet + scratch-card work)

```dart
class ApiException implements Exception {
  final int statusCode;
  final String detail;
  ApiException(this.statusCode, this.detail);

  @override
  String toString() => 'ApiException($statusCode): $detail';
}

ApiException mapDioError(dynamic e) {
  // Adjust to however your app's Dio error interceptor already normalizes errors.
  final statusCode = e.response?.statusCode ?? 0;
  final detail = e.response?.data?['detail']?.toString() ?? e.message ?? 'Unknown error';
  return ApiException(statusCode, detail);
}
```

## 3. Wallet API service

`lib/api/wallet_api.dart`

```dart
import 'package:dio/dio.dart';
import '../models/wallet.dart';
import 'api_exception.dart';

class WalletApi {
  final Dio _dio; // your app's pre-authenticated Dio instance

  WalletApi(this._dio);

  Future<Wallet> getWallet(String userId) async {
    try {
      final res = await _dio.get('/api/wallet/$userId');
      return Wallet.fromJson(res.data['wallet']);
    } on DioException catch (e) {
      throw mapDioError(e);
    }
  }

  Future<List<WalletTransaction>> getTransactions(String userId) async {
    try {
      final res = await _dio.get('/api/wallet/$userId/transactions');
      final list = res.data['transactions'] as List;
      return list.map((j) => WalletTransaction.fromJson(j)).toList();
    } on DioException catch (e) {
      throw mapDioError(e);
    }
  }

  /// [idempotencyKey]: generate ONE fresh uuid.v4() when the user taps "Redeem",
  /// and pass the SAME key back in if you retry after a timeout/network error.
  /// A fresh key per retry can (in a genuine race) queue two redeem attempts;
  /// reusing the key makes a retry a safe no-op instead.
  Future<RedeemRequest> redeem(String userId, {required String upiId, required String idempotencyKey}) async {
    try {
      final res = await _dio.post('/api/wallet/$userId/redeem', data: {
        'upiId': upiId,
        'idempotencyKey': idempotencyKey,
      });
      return RedeemRequest.fromJson(res.data['redeemRequest']);
    } on DioException catch (e) {
      throw mapDioError(e);
    }
  }
}
```

## 4. State (Provider)

`lib/providers/wallet_provider.dart`

```dart
import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';
import '../api/wallet_api.dart';
import '../api/api_exception.dart';
import '../models/wallet.dart';

enum WalletLoadState { idle, loading, loaded, error }

class WalletProvider extends ChangeNotifier {
  final WalletApi _api;
  final String userId;

  WalletProvider(this._api, this.userId);

  WalletLoadState state = WalletLoadState.idle;
  Wallet? wallet;
  List<WalletTransaction> transactions = [];
  String? errorMessage;

  // Redeem-specific state, separate from the page load state so a failed
  // redeem doesn't blank out the balance the user is already looking at.
  bool isRedeeming = false;
  String? redeemError;
  RedeemRequest? lastRedeemRequest;

  Future<void> load() async {
    state = WalletLoadState.loading;
    notifyListeners();
    try {
      final results = await Future.wait([_api.getWallet(userId), _api.getTransactions(userId)]);
      wallet = results[0] as Wallet;
      transactions = results[1] as List<WalletTransaction>;
      state = WalletLoadState.loaded;
    } catch (e) {
      errorMessage = e is ApiException ? e.detail : 'Failed to load wallet';
      state = WalletLoadState.error;
    }
    notifyListeners();
  }

  /// Returns true on success. On failure, [redeemError] is set with a
  /// user-facing message and the balance is left untouched.
  Future<bool> redeem(String upiId) async {
    isRedeeming = true;
    redeemError = null;
    notifyListeners();

    final idempotencyKey = const Uuid().v4();
    try {
      final req = await _api.redeem(userId, upiId: upiId, idempotencyKey: idempotencyKey);
      lastRedeemRequest = req;
      // Balance moved available -> reserved server-side; refresh to reflect it
      // rather than guessing the new numbers client-side.
      await load();
      return true;
    } catch (e) {
      redeemError = _redeemErrorMessage(e);
      isRedeeming = false;
      notifyListeners();
      return false;
    }
  }

  String _redeemErrorMessage(dynamic e) {
    if (e is ApiException) {
      switch (e.statusCode) {
        case 400:
          return e.detail; // "Invalid UPI ID format" or "Minimum balance of Rs.500 required"
        case 403:
          return 'Your wallet is frozen. Contact support.';
        default:
          return 'Something went wrong. Please try again.';
      }
    }
    return 'Something went wrong. Please try again.';
  }
}
```

Wire it up once you know where the logged-in user id + base Dio instance live, e.g.:

```dart
ChangeNotifierProvider(
  create: (_) => WalletProvider(WalletApi(myAuthedDio), currentUser.id)..load(),
  child: const WalletScreen(),
)
```

## 5. UI

### Wallet screen

`lib/screens/wallet_screen.dart`

```dart
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import '../providers/wallet_provider.dart';
import 'redeem_sheet.dart';

class WalletScreen extends StatelessWidget {
  const WalletScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Wallet')),
      body: Consumer<WalletProvider>(
        builder: (context, provider, _) {
          switch (provider.state) {
            case WalletLoadState.idle:
            case WalletLoadState.loading:
              return const Center(child: CircularProgressIndicator());
            case WalletLoadState.error:
              return Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(provider.errorMessage ?? 'Failed to load wallet'),
                    const SizedBox(height: 12),
                    ElevatedButton(onPressed: provider.load, child: const Text('Retry')),
                  ],
                ),
              );
            case WalletLoadState.loaded:
              final wallet = provider.wallet!;
              return RefreshIndicator(
                onRefresh: provider.load,
                child: ListView(
                  padding: const EdgeInsets.all(16),
                  children: [
                    _BalanceCard(
                      availableBalance: wallet.availableBalance,
                      reservedBalance: wallet.reservedBalance,
                      isFrozen: wallet.isFrozen,
                      onRedeemTap: wallet.isFrozen || double.parse(wallet.availableBalance) <= 0
                          ? null
                          : () => showRedeemSheet(context),
                    ),
                    const SizedBox(height: 24),
                    const Text('Transaction History', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                    const SizedBox(height: 8),
                    ...provider.transactions.map((t) => _TransactionTile(t)),
                    if (provider.transactions.isEmpty)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 32),
                        child: Center(child: Text('No transactions yet')),
                      ),
                  ],
                ),
              );
          }
        },
      ),
    );
  }
}

class _BalanceCard extends StatelessWidget {
  final String availableBalance;
  final String reservedBalance;
  final bool isFrozen;
  final VoidCallback? onRedeemTap;

  const _BalanceCard({
    required this.availableBalance,
    required this.reservedBalance,
    required this.isFrozen,
    required this.onRedeemTap,
  });

  String _rupees(String amount) => '₹${NumberFormat('#,##0.00').format(double.parse(amount))}';

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Available Balance', style: TextStyle(color: Colors.grey)),
            Text(_rupees(availableBalance), style: const TextStyle(fontSize: 32, fontWeight: FontWeight.bold)),
            if (double.parse(reservedBalance) > 0) ...[
              const SizedBox(height: 4),
              Text('${_rupees(reservedBalance)} pending payout', style: const TextStyle(color: Colors.orange)),
            ],
            if (isFrozen) ...[
              const SizedBox(height: 8),
              const Text('Wallet frozen — contact support', style: TextStyle(color: Colors.red)),
            ],
            const SizedBox(height: 16),
            SizedBox(
              width: double.infinity,
              child: ElevatedButton(onPressed: onRedeemTap, child: const Text('Redeem to UPI')),
            ),
          ],
        ),
      ),
    );
  }
}

class _TransactionTile extends StatelessWidget {
  final dynamic t; // WalletTransaction
  const _TransactionTile(this.t);

  String _label(String type) => switch (type) {
        'credit' => 'Reward credited',
        'reserve_redeem' => 'Redeem requested',
        'confirm_payout' => 'Payout sent',
        'release_reserve' => 'Redeem rejected — refunded',
        _ => type,
      };

  @override
  Widget build(BuildContext context) {
    final isCredit = t.isCredit as bool;
    return ListTile(
      leading: Icon(isCredit ? Icons.add_circle : Icons.remove_circle, color: isCredit ? Colors.green : Colors.red),
      title: Text(_label(t.type as String)),
      subtitle: Text(DateFormat('dd MMM, hh:mm a').format(t.createdAt as DateTime)),
      trailing: Text(
        '${isCredit ? '+' : '-'}₹${t.amount}',
        style: TextStyle(color: isCredit ? Colors.green : Colors.red, fontWeight: FontWeight.bold),
      ),
    );
  }
}
```

### Redeem bottom sheet

`lib/screens/redeem_sheet.dart`

```dart
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../providers/wallet_provider.dart';

void showRedeemSheet(BuildContext context) {
  showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    builder: (_) => const RedeemSheet(),
  );
}

class RedeemSheet extends StatefulWidget {
  const RedeemSheet({super.key});

  @override
  State<RedeemSheet> createState() => _RedeemSheetState();
}

class _RedeemSheetState extends State<RedeemSheet> {
  final _upiController = TextEditingController();
  final _formKey = GlobalKey<FormState>();

  // Loose client-side check only — the backend is the source of truth on
  // validity, this just avoids an obviously-wrong round trip.
  String? _validateUpi(String? value) {
    if (value == null || value.isEmpty) return 'Enter a UPI ID';
    final pattern = RegExp(r'^[\w.\-]{2,64}@[a-zA-Z]{2,64}$');
    if (!pattern.hasMatch(value)) return 'Enter a valid UPI ID, e.g. name@bank';
    return null;
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<WalletProvider>(
      builder: (context, provider, _) {
        return Padding(
          padding: EdgeInsets.only(
            left: 16, right: 16, top: 16,
            bottom: MediaQuery.of(context).viewInsets.bottom + 16,
          ),
          child: Form(
            key: _formKey,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Redeem to UPI', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Text('Available: ₹${provider.wallet?.availableBalance ?? '0.00'}'),
                const SizedBox(height: 16),
                TextFormField(
                  controller: _upiController,
                  decoration: const InputDecoration(labelText: 'UPI ID', hintText: '9876543210@ybl'),
                  validator: _validateUpi,
                ),
                if (provider.redeemError != null) ...[
                  const SizedBox(height: 8),
                  Text(provider.redeemError!, style: const TextStyle(color: Colors.red)),
                ],
                const SizedBox(height: 16),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton(
                    onPressed: provider.isRedeeming
                        ? null
                        : () async {
                            if (!_formKey.currentState!.validate()) return;
                            final ok = await provider.redeem(_upiController.text.trim());
                            if (ok && context.mounted) Navigator.pop(context);
                          },
                    child: provider.isRedeeming
                        ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Text('Confirm Redeem'),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}
```

## 6. Testing checklist before shipping

- [ ] Balance renders correctly on first load for a user with an existing wallet
- [ ] First-time wallet-screen visit for a KYC-approved user with no wallet yet
      still works (backend lazily creates it) — should not show an error
- [ ] Redeem button disabled when `availableBalance` is `"0.00"`
- [ ] Redeem with balance below ₹500 shows the backend's exact error message
- [ ] Redeem with an invalid UPI ID is caught client-side before the API call
      AND shows the backend's message if it somehow gets through
- [ ] After a successful redeem, `reservedBalance` shows and `availableBalance`
      is `"0.00"` — pull the screen to refresh and confirm it stays consistent
- [ ] Killing the app / losing network right after tapping "Confirm Redeem" and
      reopening the app does not show a duplicate pending redeem (relies on the
      idempotency key — if you added a "resume in-flight request" pattern,
      test it actually reuses the same key)
- [ ] Frozen-wallet state shows the "contact support" message, not a crash
