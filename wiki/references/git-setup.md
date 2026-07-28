# Git Setup

Initialize git in the vault to get full history and protect against bad writes.

---

## Initialize

```bash
cd "$VAULT_PATH"
git init
git add -A
git commit -m "Initial vault scaffold"
```

---

## .gitignore

The root `.gitignore` in this repo already covers the right exclusions:

```
.trash/
.DS_Store
```

---

## Remote (Optional)

To back up to GitHub:

```bash
git remote add origin https://github.com/yourname/your-vault
git push -u origin main
```

Keep the repo private if the vault contains personal notes.
