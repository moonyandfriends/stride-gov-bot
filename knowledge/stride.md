# Stride primer for proposal review

Stride is a Cosmos SDK (v0.50) chain for multichain liquid staking. Users deposit tokens from
other chains (ATOM, TIA, DYDX, INJ, ...) and receive stTokens. Delegation happens on the host
chain through interchain accounts (ICA). Host-chain balances are read back through interchain
queries (ICQ). The redemption rate (native tokens per stToken) is updated from that data. Binary:
`strided`. Source: github.com/Stride-Labs/stride. Releases are tagged `vNN.x.y`; the upgrade plan
name is `vNN`.

## Addresses
- `stride10d07y265gmmuvt4z0w9aw880jnsr700jefnezl` - gov module. The expected `authority` for
  gov-executed messages.
- `stride1k8c2m5cn322akk5wy8lpt87dd2f4yh9azg7jlh` - F5 admin (the Stride team's operations
  wallet). It and the gov module are the two entries in `utils/admins.go`.
- Any other address in an authority, admin, recipient or controller field needs explaining.

## Modules and what's sensitive
- **stakeibc** (`x/stakeibc`): host zones, validators and weights, redemption-rate bounds, ICA
  restores, trade routes, community-pool rebates, LSM. Admin- or gov-gated messages include
  RegisterHostZone, AddValidators, DeleteValidator, ChangeValidatorWeight, RebalanceValidators,
  ClearBalance, RestoreInterchainAccount, CloseDelegationChannel, CalibrateDelegation,
  UpdateInnerRedemptionRateBounds, ResumeHostZone, SetCommunityPoolRebate,
  ToggleTradeController, UpdateHostZoneParams, DeprecateHostZone, CreateTradeRoute.
  Redemption-rate bounds and host zone deletion or deprecation directly affect stToken holders.
- **records**, **icacallbacks**, **interchainquery**: accounting for deposits, unbondings and
  ICA/ICQ results. Anything that rewrites records affects user balances.
- **staketia / stakedym**: multisig-operated liquid staking for TIA and DYM (operator/safe
  addresses hold real power).
- **autopilot**: acts on incoming IBC transfer memos.
- **IBC rate limits** (ibc-go's rate-limiting app, wired in `app/app.go`; not an `x/` module):
  limits on stToken flows. Removing or raising limits reduces protection against exploits.
- **icaoracle / icqoracle / auction / strdburner / airdrop / claim / mint**: oracles, fee
  auctions, burns, emissions.
- **CosmWasm**: upload and instantiate permissions are restricted to allow-listed addresses.
  "Everybody" would be a major change.
- Stride has used interchain security and a proof-of-authority validator set. Changes to the
  validator set or consumer settings affect who can halt or censor the chain.

## What normal looks like
- Upgrades: plan `vNN` at a height a few days out, with a matching Stride-Labs release tag and
  notes. Stride upgrade proposals usually have empty `info` (no binaries attached). Validators
  build from the tag.
- Host zone housekeeping: `MsgUpdateHostZoneParams`, `MsgDeprecateHostZone`, validator changes,
  with a common.xyz/stride forum link.
- IBC client recovery for expired clients, with the substitute matching the subject's chain.
- Signaling (text) proposals from the team or community with a forum link.

## Chain queries (query_chain)
Stride's own modules are served under `/Stride-Labs/stride/<module>/...`
(e.g. `/Stride-Labs/stride/stakeibc/host_zone`). SDK, IBC and wasm modules use the usual
`/cosmos/...`, `/ibc/...` and `/cosmwasm/...` paths. To find a path, search the `.proto` files
for `option (google.api.http)`.

## Where to look in the code
- `app/upgrades/vNN/` - upgrade handlers (store migrations, balance moves, param changes).
- `app/app.go`, `app/keepers/` - module wiring and authorities.
- `x/<module>/keeper/msg_server*.go` - what each message does and who may send it.
- `x/<module>/types/params.go` - parameter defaults and validation.
- `utils/admins.go` - admin allow-list.
