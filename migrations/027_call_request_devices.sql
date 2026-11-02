-- Paid 1:1 calls: which phone holds the call, for people signed in on several phones.
-- Additive and safe to re-run. Needs 023_call_requests.sql applied first.
-- requester_device_id: the phone that answered the ring. provider_device_id: the phone that started it.
-- (Same value the app sends as the X-Device-Id header / user_devices.device_id.)

ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_device_id VARCHAR(255);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_device_id VARCHAR(255);
