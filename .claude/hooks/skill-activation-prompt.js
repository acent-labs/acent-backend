#!/usr/bin/env node
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const fs_1 = require("fs");
const path_1 = require("path");
async function main() {
    try {
        // Read input from stdin
        const input = (0, fs_1.readFileSync)(0, 'utf-8');
        const data = JSON.parse(input);
        const prompt = data.prompt.toLowerCase();
        // Load skill rules
        const projectDir = process.env.CLAUDE_PROJECT_DIR || '$HOME/project';
        const rulesPath = (0, path_1.join)(projectDir, '.claude', 'skills', 'skill-rules.json');
        const rules = JSON.parse((0, fs_1.readFileSync)(rulesPath, 'utf-8'));
        const matchedSkills = [];
        // Build set of enabled modules
        const enabledModules = new Set();
        if (rules.modules) {
            for (const [moduleName, moduleConfig] of Object.entries(rules.modules)) {
                if (moduleConfig.enabled) {
                    enabledModules.add(moduleName);
                }
            }
        }
        // Check each skill for matches
        for (const [skillName, config] of Object.entries(rules.skills)) {
            // Skip skills from disabled modules
            if (config.module && !enabledModules.has(config.module)) {
                continue;
            }
            const triggers = config.promptTriggers;
            if (!triggers) {
                continue;
            }
            // Keyword matching
            if (triggers.keywords) {
                const keywordMatch = triggers.keywords.some(kw => prompt.includes(kw.toLowerCase()));
                if (keywordMatch) {
                    matchedSkills.push({ name: skillName, matchType: 'keyword', config });
                    continue;
                }
            }
            // Intent pattern matching
            if (triggers.intentPatterns) {
                const intentMatch = triggers.intentPatterns.some(pattern => {
                    const regex = new RegExp(pattern, 'i');
                    return regex.test(prompt);
                });
                if (intentMatch) {
                    matchedSkills.push({ name: skillName, matchType: 'intent', config });
                }
            }
        }
        // Generate output if matches found
        if (matchedSkills.length > 0) {
            let output = '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n';
            output += '🎯 SKILL ACTIVATION CHECK\n';
            output += '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n';
            // Group by priority
            const critical = matchedSkills.filter(s => s.config.priority === 'critical');
            const high = matchedSkills.filter(s => s.config.priority === 'high');
            const medium = matchedSkills.filter(s => s.config.priority === 'medium');
            const low = matchedSkills.filter(s => s.config.priority === 'low');
            if (critical.length > 0) {
                output += '⚠️ CRITICAL SKILLS (REQUIRED):\n';
                critical.forEach(s => output += `  → ${s.name}\n`);
                output += '\n';
            }
            if (high.length > 0) {
                output += '📚 RECOMMENDED SKILLS:\n';
                high.forEach(s => output += `  → ${s.name}\n`);
                output += '\n';
            }
            if (medium.length > 0) {
                output += '💡 SUGGESTED SKILLS:\n';
                medium.forEach(s => output += `  → ${s.name}\n`);
                output += '\n';
            }
            if (low.length > 0) {
                output += '📌 OPTIONAL SKILLS:\n';
                low.forEach(s => output += `  → ${s.name}\n`);
                output += '\n';
            }
            output += 'ACTION: Use Skill tool BEFORE responding\n';
            output += '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n';
            console.log(output);
        }
        process.exit(0);
    }
    catch (err) {
        console.error('Error in skill-activation-prompt hook:', err);
        process.exit(1);
    }
}
main().catch(err => {
    console.error('Uncaught error:', err);
    process.exit(1);
});
