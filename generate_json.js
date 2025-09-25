import fs from 'fs';
import path from 'path';
import { globSync } from 'glob';
import { parse } from '@babel/parser';

const SOURCE_DIR = 'webtoepub_js_parsers';
const OUTPUT_FILE = 'parsers_data.json';

// Helper to find specific string return values in simple methods
function findReturnedSelector(methodBody) {
    if (!methodBody || !methodBody.body || !methodBody.body.length) return null;
    const returnStatement = methodBody.body.find(node => node.type === 'ReturnStatement');

    if (returnStatement && returnStatement.argument) {
        if (returnStatement.argument.type === 'CallExpression') {
            const callee = returnStatement.argument.callee;
            if (callee && callee.property && callee.property.name === 'querySelector' && returnStatement.argument.arguments.length > 0) {
                const arg = returnStatement.argument.arguments[0];
                if (arg.type === 'StringLiteral') {
                    return arg.value;
                }
            }
        }
        if (returnStatement.argument.type === 'StringLiteral') {
            return returnStatement.argument.value;
        }
    }
    return null;
}

function main() {
    console.log(`Starting parser data extraction from '${SOURCE_DIR}'...`);
    if (!fs.existsSync(SOURCE_DIR)) {
        console.error(`ERROR: Source directory not found: ${SOURCE_DIR}`);
        return;
    }

    const parserFiles = globSync(`${SOURCE_DIR}/*.js`);
    const allParsersData = [];

    for (const file of parserFiles) {
        const fileName = path.basename(file);
        const content = fs.readFileSync(file, 'utf-8');

        try {
            const ast = parse(content, { sourceType: 'module' });
            let className = null;
            let baseUrls = [];
            let selectors = {};

            for (const node of ast.program.body) {
                if (node.type === 'ClassDeclaration') {
                    className = node.id.name;
                    const methods = node.body.body;
                    const findContentMethod = methods.find(m => m.key.name === 'findContent');
                    const extractTitleMethod = methods.find(m => m.key.name === 'extractTitleImpl');
                    const extractAuthorMethod = methods.find(m => m.key.name === 'extractAuthor');
                    const findCoverMethod = methods.find(m => m.key.name === 'findCoverImageUrl');
                    
                    selectors.content = findReturnedSelector(findContentMethod?.body);
                    selectors.title = findReturnedSelector(extractTitleMethod?.body);
                    selectors.author = findReturnedSelector(extractAuthorMethod?.body);
                    selectors.cover = findReturnedSelector(findCoverMethod?.body);
                }

                if (node.type === 'ExpressionStatement' && node.expression.type === 'CallExpression') {
                    const callee = node.expression.callee;
                    if (callee.type === 'MemberExpression' && callee.object.name === 'parserFactory' && callee.property.name === 'register') {
                        const args = node.expression.arguments;
                        if (args.length > 0 && args[0].type === 'StringLiteral') {
                            baseUrls.push(args[0].value);
                        }
                    }
                }
            }

            if (className && baseUrls.length > 0) {
                allParsersData.push({
                    js_filename: fileName,
                    class_name: className,
                    base_urls: baseUrls,
                    selectors: selectors
                });
            } else if (className) {
                 console.warn(`WARNING: Skipping ${fileName}: Found class '${className}' but no parserFactory.register() call.`);
            }
        } catch (e) {
            // Ignore parsing errors for files that aren't modules
        }
    }

    fs.writeFileSync(OUTPUT_FILE, JSON.stringify(allParsersData, null, 2));
    console.log(`Extraction complete! Data saved to ${OUTPUT_FILE}`);
}

main();
