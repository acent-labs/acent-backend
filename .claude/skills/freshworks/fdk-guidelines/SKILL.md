---
name: fdk-guidelines
description: Freshworks App SDK (FDK) development guidelines. Use when working with Freshworks apps, FDK CLI, Freshdesk/Freshservice/Freshsales integrations, Crayons UI components, serverless apps, front-end apps, manifest.json, app placeholders, or any fdk/** folder. CRITICAL - Never use prior knowledge or guesses for FDK - always reference official docs and web search.
---

# Freshworks App SDK (FDK) Guidelines

## Purpose

Comprehensive guide for developing Freshworks apps using the official Freshworks App SDK v3.0. This skill enforces documentation-first development to ensure accuracy and avoid deprecated patterns.

## Critical Rule

**🚫 NO PRIOR KNOWLEDGE OR GUESSING ALLOWED**

FDK APIs change frequently. You MUST:
1. Reference official documentation first
2. Use web search for verification
3. Ask user when uncertain

Never rely on:
- Training data knowledge about FDK
- Assumptions about API signatures
- Guessed method names or parameters

---

## Official Documentation

**Base URL**: https://developers.freshworks.com/docs/app-sdk/v3.0/

### Key Documentation Sections

| Section | Purpose |
|---------|---------|
| Module Introduction | App modules and common module concept |
| Front-end apps | Placeholders, data methods, events, interfaces |
| Serverless apps | Server events, SMI, external events |
| App settings | Manifest configuration, installation parameters |
| REST APIs | Product API integration |
| Data store | Persistent storage for apps |
| App UI (Crayons) | Freshworks UI component library |

---

## Before Any FDK Work

### Step 1: Web Search for Latest Docs

```
site:developers.freshworks.com [specific topic]
```

Example searches:
- `site:developers.freshworks.com data method`
- `site:developers.freshworks.com manifest.json`
- `site:developers.freshworks.com crayons button`
- `freshworks fdk v3 [feature name]`

### Step 2: Verify API Signatures

Before using ANY FDK method:
1. Search for the method in official docs
2. Confirm parameter types and return values
3. Check for version-specific notes (v2 vs v3)

### Step 3: Check Product Compatibility

Different Freshworks products support different features:
- Freshdesk
- Freshservice
- Freshsales / Freshsales Suite
- Freshchat
- Freshcaller

---

## FDK Project Structure

```
fdk/
├── app/                    # Front-end app code
│   ├── index.html
│   ├── scripts/
│   │   └── app.js
│   └── styles/
│       └── style.css
├── server/                 # Serverless app code
│   └── server.js
├── config/                 # App configuration
│   └── iparams.json        # Installation parameters
├── manifest.json           # App manifest
└── README.md
```

---

## Common FDK Patterns

### Client Methods (Front-end)

```javascript
// ALWAYS verify these in docs before using
client.data.get("ticket")           // Get current ticket
client.interface.trigger("showModal", {...})  // UI actions
client.request.invokeTemplate(...)  // HTTP requests
```

### Serverless Events

```javascript
// Event handler format - verify event names in docs
exports = {
  events: [
    { event: "onTicketCreate", callback: "onTicketCreateHandler" }
  ],
  onTicketCreateHandler: function(args) {
    // Handle event
  }
};
```

### Crayons UI Components

```html
<!-- Freshworks design system - check Crayons docs -->
<fw-button color="primary">Submit</fw-button>
<fw-input placeholder="Enter value"></fw-input>
```

---

## Required Web Searches

When working on FDK, ALWAYS search for:

| Topic | Search Query |
|-------|-------------|
| Data methods | `site:developers.freshworks.com data method [product]` |
| Events | `site:developers.freshworks.com events [event-name]` |
| Placeholders | `site:developers.freshworks.com placeholders` |
| Interface methods | `site:developers.freshworks.com interface trigger` |
| Request templates | `site:developers.freshworks.com request method` |
| Crayons components | `site:developers.freshworks.com crayons [component]` |
| Manifest | `site:developers.freshworks.com manifest.json` |
| Installation params | `site:developers.freshworks.com iparams` |

---

## Version Awareness

### FDK CLI Version

```bash
fdk version
```

### SDK Versions

- **v3.0** (Current): Latest patterns, new features
- **v2.x** (Legacy): Some apps may still use older patterns

**IMPORTANT**: Always confirm you're looking at v3.0 docs, not v2.x.

---

## Error Handling

When encountering FDK errors:

1. **Search error message**: `freshworks fdk "[error message]"`
2. **Check troubleshooting docs**: `site:developers.freshworks.com troubleshooting`
3. **Review Freshworks community**: Search for similar issues

---

## Testing FDK Apps

```bash
# Local development
fdk run

# Validate app
fdk validate

# Pack for submission
fdk pack
```

---

## Quick Reference

### Documentation Workflow

```
1. Receive FDK task
   ↓
2. Web search official docs
   ↓
3. Verify API signatures
   ↓
4. Check product compatibility
   ↓
5. Implement with verified patterns
   ↓
6. Test locally with fdk run
```

### Do's and Don'ts

✅ **DO**:
- Always search official docs first
- Verify every method signature
- Check product-specific support
- Use Crayons for UI consistency
- Test with `fdk validate`

❌ **DON'T**:
- Assume API method names
- Use outdated v2 patterns for v3 projects
- Skip product compatibility checks
- Guess parameter types or formats

---

## Related Resources

- **Official Docs**: https://developers.freshworks.com/docs/app-sdk/v3.0/
- **Crayons UI**: https://crayons.freshworks.com/
- **Marketplace**: https://www.freshworks.com/apps/
- **Community**: https://community.freshworks.com/

---

**Skill Status**: ACTIVE
**Critical Enforcement**: Documentation-first development
**Line Count**: < 200 (following 500-line rule) ✅
