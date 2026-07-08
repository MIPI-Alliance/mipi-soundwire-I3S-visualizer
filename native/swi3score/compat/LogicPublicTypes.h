// Minimal standalone replacement for the Saleae SDK's LogicPublicTypes.h.
//
// The SWI3S decode classes (reused verbatim from the Saleae plugin) include
// <LogicPublicTypes.h> only for the integer typedefs + BitState. This shim
// provides exactly those, so the decode core builds with ZERO Saleae SDK
// dependency. It is placed first on the include path; the plugin build keeps
// using the real SDK header.

#ifndef LOGICPUBLICTYPES
#define LOGICPUBLICTYPES

#include <vector>
#include <memory>

#ifndef _WIN32
    #define __cdecl
#endif

#ifdef _WIN32
    #define LOGICAPI __declspec(dllexport)
    #define ANALYZER_EXPORT __declspec(dllexport)
#else
    #define LOGICAPI __attribute__((visibility("default")))
    #define ANALYZER_EXPORT __attribute__((visibility("default")))
#endif

typedef char S8;
typedef short S16;
typedef int S32;
typedef long long int S64;

typedef unsigned char U8;
typedef unsigned short U16;
typedef unsigned int U32;
typedef unsigned long long int U64;

#ifndef NULL
    #define NULL 0
#endif

enum DisplayBase { Binary, Decimal, Hexadecimal, ASCII, AsciiHex };
enum BitState { BIT_LOW, BIT_HIGH };

#define Toggle(x) ( (x) == BIT_LOW ? BIT_HIGH : BIT_LOW )
#define Invert(x) ( (x) == BIT_LOW ? BIT_HIGH : BIT_LOW )

#endif // LOGICPUBLICTYPES
