-- Paid 1:1 calls, Stage 6: the iPhone's VoIP push token (CallKit "incoming call" screen).
-- Additive and safe to re-run. Apply BEFORE deploying the Stage 6 code: the API only creates
-- missing tables at startup, it never adds columns.

ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS voip_token TEXT;
-- Debug builds run from Xcode get "sandbox" VoIP tokens, App Store / TestFlight builds get
-- "production" ones. Each must be sent to its own Apple server, so remember which.
ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS voip_sandbox BOOLEAN NOT NULL DEFAULT FALSE;
