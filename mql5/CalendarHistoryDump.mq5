//+------------------------------------------------------------------+
//|                                          CalendarHistoryDump.mq5 |
//|                                                        AutoTrade |
//+------------------------------------------------------------------+
// ONE-OFF Script (not a Service): dumps MT5's built-in economic calendar
// HISTORY -- from InpFromYear/InpFromMonth up to "now + 48h" server time --
// to a CSV in the terminal's Common Files folder
// (TERMINAL_COMMONDATA_PATH\Files\AutoTradeNewsCalendarHistory.csv).
//
// Why (EXP-024 prerequisite, see experiments/experiments_log.md's
// 2026-08-03 NOTE after EXP-023): news protection has never been
// backtestable because no historical calendar existed on disk --
// mql5/NewsCalendarExporter.mq5 only maintains a rolling [-2h, +48h]
// snapshot, and `council/calendar_archive.py` only accumulates that
// snapshot from 2026-08-03 onward. But the SAME CalendarValueHistory()
// call the exporter already uses accepts an arbitrary historical window;
// this script simply asks for the whole backtest period at once. Whether
// the terminal's calendar database actually reaches back to 2021 is an
// empirical question -- this dump answers it (see the depth summary it
// prints at the end); nothing here assumes the answer.
//
// KNOWN LIMITATION (declare in any experiment that consumes this dump):
// event importance/name reflect MetaQuotes' CURRENT classification, not
// what a terminal would have displayed at the historical moment -- a
// re-graded event carries its post-re-grade importance for all time. The
// live-accumulated archive (news_calendar_history.csv, `first_seen_utc`)
// remains the ground truth for "what did live actually see, when" from
// 2026-08-03 onward, and doubles as a cross-validation set for this dump
// on the overlap window.
//
// Conventions COPIED from NewsCalendarExporter.mq5 (same 7 CSV columns,
// same lowercase importance strings, same server-time "YYYY-MM-DD
// HH:MM:SS" format, same comma/CR/LF sanitising, same tmp-then-FileMove
// atomic write) so `mql5_calendar_provider.parse_export_csv` can read
// this file unchanged. Helpers are duplicated rather than #include'd to
// keep both files self-contained single-file programs, mirroring how the
// exporter documents its own API usage; see that file's header for the
// doc-confirmed signatures of CalendarValueHistory/CalendarEventById/
// CalendarCountryById and the server-time notes.
//
// The query is chunked by calendar month: a single 5-year unfiltered
// CalendarValueHistory() call would return one huge array (slower, harder
// to attribute failures); per-month chunks give progress logging and
// isolate any per-window calendar error to that month. Chunk ends are
// (next month start - 1s) so a value stamped exactly on a month boundary
// cannot appear in two chunks; consumers should still dedup on
// (event_time, currency, importance, event_name) like calendar_archive.py
// does, which also collapses the (rare, legitimate) duplicate-key rows.
#property script_show_inputs
#property copyright "AutoTrade"
#property version   "1.00"
#property description "One-off dump of MT5's historical economic calendar to Common Files, for EXP-024 trigger-window reconstruction."
#property strict

input int InpFromYear  = 2021;  // Dump start year (server time)
input int InpFromMonth = 7;     // Dump start month (server time)

#define DUMP_FILENAME     "AutoTradeNewsCalendarHistory.csv"
#define DUMP_FILENAME_TMP "AutoTradeNewsCalendarHistory.tmp.csv"

//+------------------------------------------------------------------+
//| Helpers duplicated from NewsCalendarExporter.mq5 (see header)     |
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

string FormatServerTime(datetime dt)
  {
   MqlDateTime s;
   TimeToStruct(dt, s);
   return StringFormat("%04d-%02d-%02d %02d:%02d:%02d", s.year, s.mon, s.day, s.hour, s.min, s.sec);
  }

string SanitizeField(string text)
  {
   StringReplace(text, ",", " ");
   StringReplace(text, "\r", " ");
   StringReplace(text, "\n", " ");
   return text;
  }

//+------------------------------------------------------------------+
//| First second of (year, month), month may be 13.. -> rolls over    |
//+------------------------------------------------------------------+
datetime MonthStart(int year, int month)
  {
   while(month > 12)
     {
      month -= 12;
      year++;
     }
   return StringToTime(StringFormat("%04d.%02d.01 00:00:00", year, month));
  }

//+------------------------------------------------------------------+
//| Script entry point                                                |
//+------------------------------------------------------------------+
void OnStart()
  {
   datetime now      = TimeTradeServer();
   datetime dump_end = now + 48 * 3600; // include the exporter's own lookahead
   datetime earliest_written = 0, latest_written = 0;

   int handle = FileOpen(DUMP_FILENAME_TMP, FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
   if(handle == INVALID_HANDLE)
     {
      PrintFormat("CalendarHistoryDump: FileOpen(%s) failed, error %d", DUMP_FILENAME_TMP, GetLastError());
      return;
     }
   FileWrite(handle, "# generated_at_server_time=" + FormatServerTime(now));
   FileWrite(handle, "event_time", "currency", "importance", "event_name", "forecast", "previous", "actual");

   int total_written = 0, failed_chunks = 0, empty_chunks = 0;
   for(int year = InpFromYear, month = InpFromMonth; MonthStart(year, month) < dump_end; month++)
     {
      if(month > 12)
        {
         month = 1;
         year++;
        }
      datetime chunk_from = MonthStart(year, month);
      datetime chunk_to   = MonthStart(year, month + 1) - 1;
      if(chunk_to > dump_end)
         chunk_to = dump_end;

      MqlCalendarValue values[];
      ResetLastError();
      int value_count = CalendarValueHistory(values, chunk_from, chunk_to, NULL, NULL);
      if(value_count < 0)
        {
         failed_chunks++;
         PrintFormat("CalendarHistoryDump: %04d-%02d FAILED (CalendarValueHistory error %d) -- continuing",
                     year, month, GetLastError());
         continue;
        }
      if(value_count == 0)
         empty_chunks++;

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
         if(earliest_written == 0 || values[i].time < earliest_written)
            earliest_written = values[i].time;
         if(values[i].time > latest_written)
            latest_written = values[i].time;
        }
      total_written += written;
      PrintFormat("CalendarHistoryDump: %04d-%02d -> %d value(s), %d written (running total %d)",
                  year, month, value_count, written, total_written);
      if(IsStopped())
        {
         Print("CalendarHistoryDump: stop requested -- aborting without replacing any existing dump file");
         FileClose(handle);
         return;
        }
     }

   FileClose(handle);

   if(!FileMove(DUMP_FILENAME_TMP, FILE_COMMON, DUMP_FILENAME, FILE_COMMON | FILE_REWRITE))
     {
      PrintFormat("CalendarHistoryDump: FileMove(%s -> %s) failed, error %d",
                  DUMP_FILENAME_TMP, DUMP_FILENAME, GetLastError());
      return;
     }

   // DEPTH SUMMARY -- the line that answers "does the terminal's calendar
   // database actually reach the requested start?": if earliest written is
   // much later than the requested start, the database is shallower than
   // the backtest window and EXP-024 must shrink its scope accordingly.
   PrintFormat(
      "CalendarHistoryDump: DONE -- %d row(s) -> %s | requested from %04d-%02d-01, earliest event written %s, latest %s | failed chunks %d, empty chunks %d",
      total_written, DUMP_FILENAME, InpFromYear, InpFromMonth,
      earliest_written == 0 ? "NONE" : FormatServerTime(earliest_written),
      latest_written == 0 ? "NONE" : FormatServerTime(latest_written),
      failed_chunks, empty_chunks
   );
  }
