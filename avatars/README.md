# Premium avatar publishing

These files are served as `https://models.dgwaynes.com/avatars/v1/*` and let a
brand-new premium avatar reach installed apps with no app release: the app
fetches `catalog.json` plus each `<key>.png` at launch and merges them into the
premium avatar set, gated by the same `avatar_entitlements` grants as the
bundled avatars.

They live in this repo purely because this is where the B2 credentials already
are. The **design master is the app repo**, `spotter-tools-pro/avatar-catalog/`,
which is private and so cannot be read by a workflow here. Treat `v1/` as a
publishing mirror of that directory.

## Publishing a change

1. Edit `avatar-catalog/` in the app repo (art plus a `catalog.json` entry).
   That README documents the id/key rules.
2. Copy the whole directory over, minus its README:

   ```bash
   cp "/c/Spotter Tools Pro WIP/avatar-catalog/"*.png \
      "/c/Spotter Tools Pro WIP/avatar-catalog/catalog.json" avatars/v1/
   ```

3. Commit, push, then run the **Publish avatars** workflow (Actions tab, Run
   workflow). It validates the catalog, uploads with `rclone copy`, and verifies
   the result over the CDN the same way the app fetches it.

No Cloudflare purge is needed. See the header comment in
`.github/workflows/publish_avatars.yml` for why, and for why the workflow uses
`copy` rather than `sync`.
