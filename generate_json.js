import fs from 'fs';
import path from 'path';
import { globSync } from 'glob';
import { parse } from '@babel/parser';

const SOURCE_DIR = 'webtoepub_js_parsers';
const OUTPUT_FILE = 'parsers_data.json';

// Helper to find specific string return values in simple methods by analyzing the AST
function findReturnedSelector(methodBody) {
    if (!methodBody || !methodBody.body || !methodBody.body.length) return null;
    const returnStatement = methodBody.body.find(node => node.type === 'ReturnStatement');

    if (returnStatement && returnStatement.argument) {
        // Case: return this.dom.querySelector('selector')
        if (returnStatement.argument.type === 'CallExpression') {
            const callee = returnStatement.argument.callee;
            if (callee && callee.property && callee.property.name === 'querySelector' && returnStatement.argument.arguments.length > 0) {
                const arg = returnStatement.argument.arguments[0];
                if (arg.type === 'StringLiteral') {
                    return arg.value;
                }
            }
        }
        // Case: return 'selector'
        if (returnStatement.argument.type === 'StringLiteral') {
            return returnStatement.argument.value;
        }
         // Case: return dom.querySelector("div.book-name");
        if(returnStatement.argument.type === 'CallExpression' && returnStatement.argument.callee.property.name === 'querySelector'){
            return returnStatement.argument.arguments[0].value;
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
            const ast = parse(content, { sourceType: 'module', plugins: ["classProperties"] });

            let className = null;
            let baseUrls = [];
            let selectors = {
                content: null,
                title: null,
                author: null,
                cover: null,
            };

            for (const node of ast.program.body) {
                // Find Class Declaration
                if (node.type === 'ClassDeclaration') {
                    className = node.id.name;
                    const methods = node.body.body;
                    
                    const findContentMethod = methods.find(m => m.key.name === 'findContent');
                    const extractTitleMethod = methods.find(m => m.key.name === 'extractTitleImpl');
                    const extractAuthorMethod = methods.find(m => m.key.name === 'extractAuthor');
                    const findCoverMethod = methods.find(m => m.key.name === 'findCoverImageUrl');

                    if (findContentMethod) selectors.content = findReturnedSelector(findContentMethod.body);
                    if (extractTitleMethod) selectors.title = findReturnedSelector(extractTitleMethod.body);
                    if (extractAuthorMethod) selectors.author = findReturnedSelector(extractAuthorMethod.body);
                    if (findCoverMethod) selectors.cover = findReturnedSelector(findCoverMethod.body);
                }

                // Find parserFactory.register() calls
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
                 console.warn(`WARNING: Skipping ${fileName}: Found class '${className}' but could not find any parserFactory.register() calls.`);
            }

        } catch (e) {
            // This can happen for non-module files, which we can safely ignore.
            // console.error(`Could not parse ${fileName}: ${e.message}`);
        }
    }

    fs.writeFileSync(OUTPUT_FILE, JSON.stringify(allParsersData, null, 2));
    console.log(`\nExtraction complete! Data saved to ${OUTPUT_FILE}`);
}

main();
