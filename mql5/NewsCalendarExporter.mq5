//+------------------------------------------------------------------+
//|                                       NewsCalendarExporter.mq5   |
//|                                                        AutoTrade |
//+------------------------------------------------------------------+
// Periodically exports MT5's built-in, free economic calendar to a CSV
// file in the terminal's shared Common Files folder
// (TERMINAL_COMMONDATA_PATH\Files\), so that this repo's Python side
// (src/autotrade/council/mql5_calendar_provider.py's MQL5CalendarProvider)
// can read it without needing any of the 6 MQL5-only calendar functions
// itself (the official `MetaTrader5` Python package does not expose them --
// see council/news_calendar.py's module docstring for the full history of
// why a real NewsCalendarProvider was needed and what was tried first).
//
// This is an MQL5 *Service* (#property service below), not a Script or EA:
// per MQL5's docs (Program Running), a service is not bound to any chart
// and keeps running in the background for as long as it is started from
// the terminal's Navigator panel -- the right fit for "keep re-exporting
// the calendar periodically while the terminal is open" (this trading
// system already keeps the terminal running continuously). Services only
// get a single OnStart() event handler (no OnInit/OnDeinit/OnTimer/OnTick);
// the standard pattern is an internal `while(!IsStopped())` loop with a
// `Sleep()` between iterations -- IsStopped()/Sleep() confirmed against
// MQL5's real docs (mql5.com/en/docs/check/isstopped,
// mql5.com/en/docs/common/sleep -- Sleep() itself already polls the stop
// flag every 0.1s internally, so a single Sleep(N minutes) call per loop
// iteration is still promptly interruptible when the service is stopped
// from the Navigator).
//
// Calendar functions used here (signatures confirmed against MQL5's real
// docs at mql5.com/en/docs/calendar and the MqlCalendarEvent/
// MqlCalendarValue/MqlCalendarCountry structures at
// mql5.com/en/docs/constants/structures/mqlcalendar -- NOT guessed from
// memory, per this task's own instruction that MQL5 syntax mistakes are
// likely):
//
//   int  CalendarValueHistory(MqlCalendarValue &values[], datetime from,
//                              datetime to=0, string country_code=NULL,
//                              string currency=NULL)
//       -- the single broadest calendar query: all event *values* (i.e.
//       one row per scheduled/published data point, with its own actual/
//       forecast/previous numbers) in a time window, optionally filtered
//       by country/currency. This service passes NULL/NULL (no filter) and
//       exports every country/currency broadly, per this task's own
//       guidance -- filtering down to the 4 currencies risk_voice.py cares
//       about (USD/EUR/GBP/JPY) happens on the Python side instead, which
//       keeps this exporter simple and robust to that currency list
//       changing later without a recompile.
//   bool CalendarEventById(ulong event_id, MqlCalendarEvent &event)
//       -- MqlCalendarValue only carries an event_id; the event's own name
//       and importance rating live on the separate MqlCalendarEvent
//       structure, looked up per value here.
//   bool CalendarCountryById(long country_id, MqlCalendarCountry &country)
//       -- MqlCalendarEvent only carries a country_id; the actual currency
//       code (e.g. "USD") lives on the separate MqlCalendarCountry
//       structure, looked up per event here.
//
// Confirmed from the same docs: ALL calendar functions/timestamps use the
// trade server's own time (TimeTradeServer()), not true UTC and not local
// time -- this matches common/mt5_time.ServerClock, the clock this
// repo's ShadowLoop/WatchmanLoop actually run on, so the exported
// event_time column is written as-is (server time, no conversion) and the
// Python side must NOT attach a UTC tzinfo to it (see
// mql5_calendar_provider.py's module docstring for why this differs from
// council/finnhub_news_calendar.py's UTC-tagging convention).
//
// ENUM_CALENDAR_EVENT_IMPORTANCE (confirmed values, in ascending order):
// CALENDAR_IMPORTANCE_NONE, CALENDAR_IMPORTANCE_LOW,
// CALENDAR_IMPORTANCE_MODERATE, CALENDAR_IMPORTANCE_HIGH -- exported as the
// lowercase strings "none"/"low"/"moderate"/"high" below, matching
// council/finnhub_news_calendar.py's own "high" (case-insensitive) impact
// string convention so the Python side's high-impact filter reads the same
// literal either way.
//
// Atomic-write handling: written in one shot per cycle (not incrementally)
// to a temp filename first, then FileMove()'d over the real export
// filename with FILE_REWRITE -- confirmed via mql5.com/en/docs/files/
// filemove that FileMove() is a single call (not a read-then-write pair),
// so Python never observes a half-written file: at any instant the real
// filename either has last cycle's complete file or this cycle's complete
// file, never a partial one.
#property service
#property copyright "AutoTrade"
#property version   "1.00"
#property description "Exports MT5's built-in economic calendar to a CSV file in the Common Files folder, for autotrade.council.mql5_calendar_provider.MQL5CalendarProvider to read."
#property strict

input int InpExportIntervalMinutes = 5;   // How often to re-export the calendar (minutes)
input int InpLookbackHours         = 2;   // How far back (server time) to include events in the export
input int InpLookaheadHours        = 48;  // How far forward (server time) to include events in the export

#define EXPORT_FILENAME     "AutoTradeNewsCalendar.csv"
#define EXPORT_FILENAME_TMP "AutoTradeNewsCalendar.tmp.csv"

//+------------------------------------------------------------------+
//| CALENDAR_IMPORTANCE_* -> lowercase string, matching                |
//| finnhub_news_calendar.py's "high" (case-insensitive) convention    |
//+------------------------------------------------------------------+
string ImportanceToString(ENUM_CALENDAR_EVENT_IMPORTANCE importance)
  {
   switch(importance)
     {
      case CALENDAR_IMPORTANCE_HIGH:
         return "high";
      case CALENDAR_IMPORTANCE_MODERATE:
         return "moderate";
      case CALENDAR_IMPORTANCE_LOW:
         return "low";
      default:
         return "none";
     }
  }

//+------------------------------------------------------------------+
//| "YYYY-MM-DD HH:MM:SS" (server time, unambiguous for Python's       |
//| strptime) -- deliberately not MQL5's own default "YYYY.MM.DD       |
//| HH:MM:SS" TimeToString() format, to avoid any '.' vs '-' ambiguity |
//+------------------------------------------------------------------+
string FormatServerTime(datetime dt)
  {
   MqlDateTime s;
   TimeToStruct(dt, s);
   return StringFormat("%04d-%02d-%02d %02d:%02d:%02d", s.year, s.mon, s.day, s.hour, s.min, s.sec);
  }

//+------------------------------------------------------------------+
//| Strips characters that would otherwise break the fixed 7-column    |
//| CSV shape (event names are free text and could in principle        |
//| contain a comma; MQL5's FileWrite()/FILE_CSV does not itself quote |
//| or escape field contents -- confirmed against mql5.com/en/docs/    |
//| files/filewrite) -- the Python side additionally treats any row    |
//| that still fails to parse as a single malformed row to skip, not a |
//| reason to abort the whole file, but sanitizing here avoids that in |
//| the overwhelming common case.                                     |
//+------------------------------------------------------------------+
string SanitizeField(string text)
  {
   StringReplace(text, ",", " ");
   StringReplace(text, "\r", " ");
   StringReplace(text, "\n", " ");
   return text;
  }

//+------------------------------------------------------------------+
//| One export cycle: query the calendar, write it to a temp file,    |
//| then FileMove() it over the real export filename. Returns the     |
//| number of rows written, or -1 on failure (logged to the Experts   |
//| journal either way).                                              |
//+------------------------------------------------------------------+
int ExportCalendar()
  {
   datetime now       = TimeTradeServer();
   datetime date_from = now - InpLookbackHours * 3600;
   datetime date_to   = now + InpLookaheadHours * 3600;

   MqlCalendarValue values[];
   int value_count = CalendarValueHistory(values, date_from, date_to, NULL, NULL);
   if(value_count < 0)
     {
      PrintFormat("NewsCalendarExporter: CalendarValueHistory() failed, error %d", GetLastError());
      return -1;
     }

   int handle = FileOpen(EXPORT_FILENAME_TMP, FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
   if(handle == INVALID_HANDLE)
     {
      PrintFormat("NewsCalendarExporter: FileOpen(%s) failed, error %d", EXPORT_FILENAME_TMP, GetLastError());
      return -1;
     }

   // First line: export timestamp, for a human eyeballing the file in the
   // Common Files folder. The Python side's actual staleness check uses
   // the file's own OS last-modified time instead (more robust than
   // re-parsing this line -- see mql5_calendar_provider.py), but logging
   // it here too costs nothing and helps debugging from within MT5.
   FileWrite(handle, "# generated_at_server_time=" + FormatServerTime(now));
   FileWrite(handle, "event_time", "currency", "importance", "event_name", "forecast", "previous", "actual");

   int written = 0;
   for(int i = 0; i < value_count; i++)
     {
      MqlCalendarEvent event;
      if(!CalendarEventById(values[i].event_id, event))
         continue;

      MqlCalendarCountry country;
      if(!CalendarCountryById(event.country_id, country))
         continue;
      if(country.currency == "")
         continue;

      FileWrite(
         handle,
         FormatServerTime(values[i].time),
         country.currency,
         ImportanceToString(event.importance),
         SanitizeField(event.name),
         values[i].HasForecastValue() ? DoubleToString(values[i].GetForecastValue(), 6) : "",
         values[i].HasPreviousValue() ? DoubleToString(values[i].GetPreviousValue(), 6) : "",
         values[i].HasActualValue()   ? DoubleToString(values[i].GetActualValue(), 6)   : ""
      );
      written++;
     }

   FileClose(handle);

   if(!FileMove(EXPORT_FILENAME_TMP, FILE_COMMON, EXPORT_FILENAME, FILE_COMMON | FILE_REWRITE))
     {
      PrintFormat("NewsCalendarExporter: FileMove(%s -> %s) failed, error %d",
                  EXPORT_FILENAME_TMP, EXPORT_FILENAME, GetLastError());
      return -1;
     }

   PrintFormat(
      "NewsCalendarExporter: exported %d/%d calendar value(s) to %s (window %s .. %s server time)",
      written, value_count, EXPORT_FILENAME, FormatServerTime(date_from), FormatServerTime(date_to)
   );
   return written;
  }

//+------------------------------------------------------------------+
//| Service entry point -- MQL5 services only get this one event      |
//| handler (confirmed via mql5.com/en/docs/runtime/running); the      |
//| endless loop + IsStopped()/Sleep() pattern below is that page's    |
//| own documented model for a looping service.                       |
//+------------------------------------------------------------------+
void OnStart()
  {
   PrintFormat(
      "NewsCalendarExporter: service starting -- export every %d min, window [-%dh, +%dh] server time",
      InpExportIntervalMinutes, InpLookbackHours, InpLookaheadHours
   );

   while(!IsStopped())
     {
      ExportCalendar();
      Sleep(InpExportIntervalMinutes * 60 * 1000);
     }

   PrintFormat("NewsCalendarExporter: service stopping (stop requested)");
  }
