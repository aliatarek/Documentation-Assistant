<#
Starts the Documentation Assistant and, when requested, securely prompts for an
Anthropic API key. The key is held only in this PowerShell process while the
server runs; it is not written to source code, the browser, or a project file.

Usage:
  .\start_documentation_assistant.ps1
  .\start_documentation_assistant.ps1 -UseClaude
#>
param([switch]$UseClaude)

Set-Location $PSScriptRoot

if ($UseClaude -and [string]::IsNullOrWhiteSpace($env:ANTHROPIC_API_KEY)) {
    $secureKey = Read-Host 'Paste the Anthropic API key (input is hidden)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

python -m gold_docs.server --open-browser
