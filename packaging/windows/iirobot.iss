; iiRobot Windows installer (Inno Setup 6).
; Build on Windows with:
;   powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
;
; The build script prepares build\windows\app (source, Windows wheels, tools,
; bundled Python installer) and then calls ISCC.exe with this file.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{6D5B7E6E-4C1A-4F0B-9B5A-2E6B9A6F1C21}
AppName=iiRobot
AppVersion={#MyAppVersion}
AppPublisher=iiRobot
AppPublisherURL=https://synks.ru
DefaultDirName={localappdata}\Programs\iiRobot
DefaultGroupName=iiRobot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\..\dist
OutputBaseFilename=iiRobot-{#MyAppVersion}-Setup
SetupIconFile=..\..\static\app_icon.ico
UninstallDisplayIcon={app}\static\app_icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
SetupLogging=yes
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce
Name: "voicemodels"; Description: "Download Russian voice models T-one + GigaAM (~450 MB)"; GroupDescription: "Voice input:"
Name: "llm"; Description: "Install Ollama and a local LLM model for voice correction"; GroupDescription: "Voice correction:"; Flags: unchecked

[Files]
Source: "..\..\build\windows\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\iiRobot"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\static\app_icon.ico"
Name: "{autodesktop}\iiRobot"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\static\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install-windows.ps1"" -FromSetup -AppDir ""{app}"" -WheelsDir ""{app}\wheels"" -ToolsDir ""{app}\tools"" -PythonInstaller ""{app}\python-installer.exe"" -Llm ""{code:LlmMode}"" -VoiceModels ""{code:VoiceMode}"" -NoShortcuts -Unattended"; StatusMsg: "Installing Python runtime, dependencies and selected models..."; Flags: waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
var
  LlmPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  LlmPage := CreateInputOptionPage(wpSelectTasks,
    'Local LLM for voice correction',
    'Choose a model for the local text corrector',
    'Ollama downloads the selected model. You can switch or unload models later in Settings.',
    False, False);
  LlmPage.Add('No local LLM');
  LlmPage.Add('qwen3.5:4b  (~3.1 GB, lower VRAM)');
  LlmPage.Add('qwen2.5:7b  (~4.4 GB)');
  LlmPage.Add('Both models');
  LlmPage.SelectedValueIndex := 0;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = LlmPage.ID) and (not WizardIsTaskSelected('llm'));
end;

function LlmMode(Param: String): String;
begin
  if not WizardIsTaskSelected('llm') then
    Result := 'none'
  else
    case LlmPage.SelectedValueIndex of
      1: Result := 'small';
      2: Result := 'default';
      3: Result := 'both';
    else
      Result := 'none';
    end;
end;

function VoiceMode(Param: String): String;
begin
  if WizardIsTaskSelected('voicemodels') then
    Result := 'yes'
  else
    Result := 'no';
end;
