"""List the offline Windows (SAPI) voices CHAOS can speak with."""
import comtypes.client


def main():
    sapi_voice = comtypes.client.CreateObject("SAPI.SpVoice")
    for token in sapi_voice.GetVoices():
        try:
            gender = token.GetAttribute("Gender")
        except Exception:
            gender = "?"
        print(f"{token.GetDescription()} | {gender}")


if __name__ == "__main__":
    main()
