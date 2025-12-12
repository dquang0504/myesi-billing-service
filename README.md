# myesi-billing-service

## Lemon Squeezy configuration

The billing service now uses Lemon Squeezy hosted checkout sessions. Set the
following environment variables (e.g. in `.env` or container secrets):

| Variable | Description |
| --- | --- |
| `LEMONSQUEEZY_API_KEY` | API key generated in the Lemon Squeezy dashboard. Required for every API call. |
| `LEMONSQUEEZY_STORE_ID` | Store identifier used when creating checkouts. |
| `LEMONSQUEEZY_DEFAULT_VARIANT_ID` | Variant that maps to the default subscription plan when one is not supplied explicitly. |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | Secret used to verify `X-Signature` headers on incoming Lemon Squeezy webhook payloads. |
| `LEMONSQUEEZY_CHECKOUT_SUCCESS_URL` | URL the hosted checkout redirects to on success. Defaults to the local admin success page. |
| `LEMONSQUEEZY_CHECKOUT_CANCEL_URL` | URL used when the customer cancels out of the hosted checkout. |
| `LEMONSQUEEZY_API_BASE` | Optional override for the API base (defaults to `https://api.lemonsqueezy.com/v1`). |

Use the new `app/utils/lemonsqueezy_client.py` helper to interact with the API,
create checkout sessions, retrieve variants, and verify webhook HMAC signatures.
